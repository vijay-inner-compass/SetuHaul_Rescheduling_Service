"""Run ONE controlled scenario against a real model and capture its trace.

Needs an approved provider and key supplied through the environment (never the repository):
    SETUHAUL_MODEL_PROVIDER=anthropic  SETUHAUL_MODEL_ID=<approved model id>  SETUHAUL_MODEL_API_KEY=<key>
Optional, so the cost stop is meaningful:  SETUHAUL_MODEL_PRICE_IN_PER_MTOK / SETUHAUL_MODEL_PRICE_OUT_PER_MTOK.

Without them this exits with code 3 and does nothing. The raw trace goes to live_traces/ (git-ignored);
a sanitised copy goes to tests/traces/live-<id>.json for deterministic replay in CI.
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from setuhaul.model_gateway import ModelUnavailable, build_gateway, load_model_policy

TURNS = [
    "SHP1006 has broken down, the repair will take about 90 minutes",
    "after the repair the drive to the warehouse is about 45 minutes",
    "I will take the first option",
    "yes please confirm that slot",
]


def main() -> int:
    policy = load_model_policy()
    if not (policy.provider and policy.model_id and os.environ.get("SETUHAUL_MODEL_API_KEY")):
        print("BLOCKED: no approved model configured. Set SETUHAUL_MODEL_PROVIDER, SETUHAUL_MODEL_ID and "
              "SETUHAUL_MODEL_API_KEY in the environment (decision Q9). Nothing was called.", file=sys.stderr)
        return 3
    try:
        gateway = build_gateway(policy)
    except ModelUnavailable as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 3

    import fakeredis
    from tests.helpers import at, seed_everything_known

    from setuhaul.booking import make_hold_checker
    from setuhaul.clock import FakeClock
    from setuhaul.config import load_settings
    from setuhaul.conversation import (
        ConversationServices,
        InboundMessage,
        TrustedConversationContext,
        handle_message,
    )
    from setuhaul.db import make_working_db, open_connection
    from setuhaul.escalation import InMemoryEscalationSink, JsonlEscalationSink
    from setuhaul.evaluation import (
        RecordingGateway,
        Trace,
        build_turn_trace,
        classify_failure,
        run_evaluators,
        sanitize_trace,
    )
    from setuhaul.identity import TrustedDriverContext, normalize_phone
    from setuhaul.memory import ConversationMemoryStore

    d = Path(tempfile.mkdtemp(prefix="setuhaul-live-"))
    conn = open_connection(path=make_working_db(d / "live.sqlite3"))
    seed_everything_known(conn)  # labelled TEST_FIXTURE labour and consignee evidence
    clock = FakeClock(at("09:35"))
    memory = ConversationMemoryStore(conn, fakeredis.FakeRedis(decode_responses=True), clock, load_settings(),
                                     hold_checker=make_hold_checker(conn, clock))
    row = conn.execute("select carrier_id, phone from drivers where driver_id='DRV006'").fetchone()
    trusted = TrustedConversationContext(driver=TrustedDriverContext(driver_id="DRV006", carrier_id=row["carrier_id"],
                                         phone_e164=normalize_phone(row["phone"])), thread_id="THR001")
    sink = InMemoryEscalationSink()
    trace = Trace(trace_id=f"live-{uuid.uuid4().hex[:8]}", source="live", model={"provider": policy.provider, "model_id": policy.model_id},
                  scenario="repair-clarify-options-hold-confirm", driver_id="DRV006", thread_id="THR001",
                  clock_start=clock.now().isoformat())
    for n, text in enumerate(TURNS, start=1):
        clock.advance(timedelta(seconds=20))  # the driver takes a moment between messages
        rec = RecordingGateway(gateway)
        services = ConversationServices(conn=conn, memory=memory, gateway=rec, policy=policy, sink=sink,
                                        fallback_sink=JsonlEscalationSink(d / "fallback.jsonl"))
        before = len(sink.requests)
        reply = handle_message(InboundMessage(message_id=f"LIVE-{n}", text=text), trusted, clock, services)
        keys = list(sink.requests[-1].context) if len(sink.requests) > before else []
        trace.turns.append(build_turn_trace(n, f"LIVE-{n}", text, rec.drain(), reply, keys, 20))
        print(f"turn {n}: DRIVER {text!r}\n        SERVICE {reply.text!r} [{reply.status}] tools={reply.tools_executed}")

    raw_dir = ROOT / "live_traces"
    raw_dir.mkdir(exist_ok=True)
    (raw_dir / f"{trace.trace_id}.raw.json").write_text(trace.model_dump_json(indent=2), encoding="utf-8")
    clean = sanitize_trace(trace)
    out = ROOT / "tests" / "traces" / f"{clean.trace_id}.json"
    out.write_text(clean.model_dump_json(indent=2), encoding="utf-8")
    findings = run_evaluators(clean)
    print(f"\nsanitised trace: {out}\nevaluators: {sum(f.ok for f in findings)}/{len(findings)} passed")
    for f in findings:
        if not f.ok:
            print("  FAIL", f)
    for t in clean.turns:
        print(f"  turn {t.turn} failure classes: {classify_failure(t) or '-'}")
    print("READ THE FULL TRACE before deriving new evaluations from it.")
    return 0 if all(f.ok for f in findings) else 1


if __name__ == "__main__":
    raise SystemExit(main())
