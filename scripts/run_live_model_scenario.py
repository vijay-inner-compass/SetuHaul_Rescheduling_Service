"""Run ONE controlled scenario against a real model and capture its trace.

Needs an approved provider and key supplied through the environment (never the repository):
    SETUHAUL_MODEL_PROVIDER=openai|anthropic  SETUHAUL_MODEL_ID=<approved model id>  SETUHAUL_MODEL_API_KEY=<key>
Optional, so the cost stop is meaningful:  SETUHAUL_MODEL_PRICE_IN_PER_MTOK / SETUHAUL_MODEL_PRICE_OUT_PER_MTOK.

Without them this exits with code 3 and does nothing. The raw trace goes to live_traces/ (git-ignored);
a sanitised copy goes to tests/traces/live-<id>.json for deterministic replay in CI.
"""

# Lazy annotations.
from __future__ import annotations

# Environment variables.
import os

# sys.path and stderr.
import sys

# Scratch folder.
import tempfile

# Trace ids.
import uuid

# Time between messages.
from datetime import timedelta

# Path arithmetic.
from pathlib import Path

# The repository root.
ROOT = Path(__file__).resolve().parents[1]
# Make tests helpers importable.
sys.path.insert(0, str(ROOT))
# Make the service importable.
sys.path.insert(0, str(ROOT / "src"))

# Model configuration helpers.
from setuhaul.model_gateway import ModelUnavailable, build_gateway, load_model_policy

# The four driver messages for each language. Same story: breakdown, drive time, pick option 1, confirm.
SCENARIOS = {
    "english": [
        "SHP1006 has broken down, the repair will take about 90 minutes",
        "after the repair the drive to the warehouse is about 45 minutes",
        "I will take the first option",
        "yes please confirm that slot",
    ],
    "hinglish": [
        "SHP1006 ki gaadi kharab ho gayi hai, repair mein lagbhag 90 minute lagenge",
        "repair ke baad warehouse tak drive lagbhag 45 minute ka hai",
        "pehla option theek hai, wahi le lo",
        "haan, confirm kar do",
    ],
    "hindi": [
        "SHP1006 का ट्रक खराब हो गया है, मरम्मत में करीब 90 मिनट लगेंगे",
        "मरम्मत के बाद गोदाम तक ड्राइव करीब 45 मिनट की है",
        "पहला विकल्प ठीक है",
        "हाँ, कन्फर्म कर दीजिए",
    ],
}


def load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines from a git-ignored .env without overriding the real environment. Values are never printed."""
    # Tests set this so they can never reach a provider.
    if os.environ.get("SETUHAUL_DOTENV") == "off" or not path.exists():  # tests set this so they can never reach a provider
        return
    # Each line of .env.
    for line in path.read_text(encoding="utf-8").splitlines():
        # Trim.
        line = line.strip()
        # Skip blanks and comments.
        if line and not line.startswith("#") and "=" in line:
            # Key and value.
            k, v = line.split("=", 1)
            # Set only if not already set. Values are never printed.
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# Run the scenario against a real model.
def main() -> int:
    # Command-line options.
    import argparse

    # One option: --language.
    ap = argparse.ArgumentParser()
    # english, hinglish or hindi.
    ap.add_argument("--language", choices=sorted(SCENARIOS), default="english")
    # The chosen language.
    lang = ap.parse_args().language
    # Its driver messages.
    turns = SCENARIOS[lang]
    # Load configuration from .env.
    load_dotenv(ROOT / ".env")
    # Provider, model id and limits.
    policy = load_model_policy()
    # No approved model: stop, doing nothing.
    if not (policy.provider and policy.model_id and os.environ.get("SETUHAUL_MODEL_API_KEY")):
        # Say what is missing (exit code 3).
        print("BLOCKED: no approved model configured. Set SETUHAUL_MODEL_PROVIDER, SETUHAUL_MODEL_ID and "
              "SETUHAUL_MODEL_API_KEY in the environment (decision Q9). Nothing was called.", file=sys.stderr)
        # Nothing was called.
        return 3
    # Optional tracing.
    from setuhaul.langsmith_trace import build_tracer, flush_tracing, tracing_status

    # Say whether tracing is on.
    print("LangSmith tracing:", tracing_status().reason)
    # Build the provider adapter.
    try:
        # The adapter for the configured provider.
        gateway = build_gateway(policy)
    # Unsupported provider: stop.
    except ModelUnavailable as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 3

    # In-process fake cache.
    import fakeredis

    # Time helper and standard test evidence.
    from tests.helpers import at, seed_everything_known

    # Live hold checker.
    from setuhaul.booking import make_hold_checker

    # Fixed clock.
    from setuhaul.clock import FakeClock

    # Settings.
    from setuhaul.config import load_settings

    # The conversation workflow.
    from setuhaul.conversation import (
        ConversationServices,
        InboundMessage,
        TrustedConversationContext,
        handle_message,
    )

    # Database helpers.
    from setuhaul.db import make_working_db, open_connection

    # Hand-off sinks.
    from setuhaul.escalation import InMemoryEscalationSink, JsonlEscalationSink

    # Trace recording, sanitising and scoring.
    from setuhaul.evaluation import (
        RecordingGateway,
        Trace,
        build_turn_trace,
        classify_failure,
        run_evaluators,
        sanitize_trace,
    )

    # Trusted driver context.
    from setuhaul.identity import TrustedDriverContext, normalize_phone

    # Conversation memory.
    from setuhaul.memory import ConversationMemoryStore

    # Scratch folder.
    d = Path(tempfile.mkdtemp(prefix="setuhaul-live-"))
    # A fresh copy of the seed database.
    conn = open_connection(path=make_working_db(d / "live.sqlite3"))
    # Labelled TEST_FIXTURE evidence. Only synthetic data is sent to the provider.
    seed_everything_known(conn)  # labelled TEST_FIXTURE labour and consignee evidence
    # Fixed start time.
    clock = FakeClock(at("09:35"))
    # Fresh memory with fake Redis.
    memory = ConversationMemoryStore(conn, fakeredis.FakeRedis(decode_responses=True), clock, load_settings(),
                                     hold_checker=make_hold_checker(conn, clock))
    # Driver DRV006's carrier and phone.
    row = conn.execute("select carrier_id, phone from drivers where driver_id='DRV006'").fetchone()
    # Trusted identity for that driver.
    trusted = TrustedConversationContext(driver=TrustedDriverContext(driver_id="DRV006", carrier_id=row["carrier_id"],
                                         phone_e164=normalize_phone(row["phone"])), thread_id="THR001")
    # Hand-offs are kept in memory.
    sink = InMemoryEscalationSink()
    # The trace being recorded.
    trace = Trace(trace_id=f"live-{uuid.uuid4().hex[:8]}", source="live", model={"provider": policy.provider, "model_id": policy.model_id},
                  scenario=f"repair-clarify-options-hold-confirm-{lang}", driver_id="DRV006", thread_id="THR001",
                  clock_start=clock.now().isoformat())
    # Send each driver message.
    for n, text in enumerate(turns, start=1):
        # The driver takes a moment between messages.
        clock.advance(timedelta(seconds=20))  # the driver takes a moment between messages
        # Record every model call.
        rec = RecordingGateway(gateway)
        # The real workflow with the real model.
        services = ConversationServices(conn=conn, memory=memory, gateway=rec, policy=policy, sink=sink,
                                        fallback_sink=JsonlEscalationSink(d / "fallback.jsonl"))
        # Detect new hand-offs.
        before = len(sink.requests)
        # One turn.
        reply = handle_message(InboundMessage(message_id=f"LIVE-{n}", text=text), trusted, clock, services)
        # Context keys of any new hand-off.
        keys = list(sink.requests[-1].context) if len(sink.requests) > before else []
        # Add the turn to the trace.
        trace.turns.append(build_turn_trace(n, f"LIVE-{n}", text, rec.drain(), reply, keys, 20))
        # Show what the driver said and what the service replied.
        print(f"turn {n}: DRIVER {text!r}\n        SERVICE {reply.text!r} [{reply.status}] tools={reply.tools_executed}")

    # Where raw traces go (git-ignored).
    raw_dir = ROOT / "live_traces"
    # Create it.
    raw_dir.mkdir(exist_ok=True)
    # Save the raw trace.
    (raw_dir / f"{trace.trace_id}.raw.json").write_text(trace.model_dump_json(indent=2), encoding="utf-8")
    # Stable ids, no personal data.
    clean = sanitize_trace(trace)
    # Where the sanitised trace goes (committed, for CI replay).
    out = ROOT / "tests" / "traces" / f"{clean.trace_id}.json"
    # Save it.
    out.write_text(clean.model_dump_json(indent=2), encoding="utf-8")
    # Score it with the deterministic evaluators.
    findings = run_evaluators(clean)
    # Report where it is and how many checks passed.
    print(f"\nsanitised trace: {out}\nevaluators: {sum(f.ok for f in findings)}/{len(findings)} passed")
    # List any failed checks.
    for f in findings:
        if not f.ok:
            print("  FAIL", f)
    # List each turn's failure classes.
    for t in clean.turns:
        print(f"  turn {t.turn} failure classes: {classify_failure(t) or '-'}")
    # Send queued LangSmith runs before exiting (no-op unless opted in).
    flush_tracing(build_tracer())  # send any queued LangSmith runs before the process exits (no-op unless opted in)
    # A reminder: judge behaviour by reading the trace, not just the score.
    print("READ THE FULL TRACE before deriving new evaluations from it.")
    # Exit non-zero if any evaluator failed.
    return 0 if all(f.ok for f in findings) else 1


# Run main when executed as a script.
if __name__ == "__main__":
    raise SystemExit(main())
