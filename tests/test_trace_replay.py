"""Phase 10: sanitised traces replay through the deterministic layers, and the evaluators can fail."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import fakeredis
import httpx
import pytest

from setuhaul.db import make_working_db, open_connection
from setuhaul.evaluation import (
    RecordingGateway,
    RoundTrace,
    ToolEvent,
    Trace,
    TurnTrace,
    classify_failure,
    eval_explicit_approval,
    eval_handoff_complete,
    eval_identity,
    eval_option_set_ownership,
    eval_round_caps,
    eval_state_backed_wording,
    eval_tool_order,
    replay_trace,
    run_evaluators,
    sanitize_trace,
)
from setuhaul.model_gateway import ModelPolicy, ModelResponse, PermanentModelError, ScriptedGateway
from tests.helpers import seed_everything_known

ROOT = Path(__file__).resolve().parents[1]
TRACES = sorted((ROOT / "tests" / "traces").glob("*.json"))
POLICY = ModelPolicy(provider="scripted", model_id="s")


def load(path: Path) -> Trace:
    return Trace.model_validate_json(path.read_text(encoding="utf-8"))


def fresh_world(tmp_path: Path):  # type: ignore[no-untyped-def]
    conn = open_connection(path=make_working_db(tmp_path / "w.sqlite3"))
    seed_everything_known(conn)
    return conn, fakeredis.FakeRedis(decode_responses=True)


def mk_turn(turn: int = 1, text: str = "hello", events: list[ToolEvent] | None = None, reply: str = "ok", **kw: object) -> TurnTrace:
    return TurnTrace(turn=turn, message_id=f"M{turn}", driver_text=text, rounds=[RoundTrace(tool_events=events or [])],
                     reply_text=reply, reply_status=str(kw.pop("reply_status", "REPLIED")), **kw)  # type: ignore[arg-type]


def mk_trace(*turns: TurnTrace) -> Trace:
    return Trace(trace_id="t", source="scripted", scenario="s", driver_id="DRV006", thread_id="THR001",
                 clock_start="2026-08-04T09:35:00+05:30", turns=list(turns))


def ev(name: str, args: dict | None = None, result: dict | None = None, executed: bool = True) -> ToolEvent:  # type: ignore[type-arg]
    return ToolEvent(call_id="c", name=name, arguments=args or {}, result=result, executed=executed,
                     error=(result or {}).get("error"))


# --- the committed baselines ---------------------------------------------------------------------------
def test_baseline_traces_exist_and_are_labelled_scripted() -> None:
    assert len(TRACES) >= 5
    assert {load(p).source for p in TRACES} <= {"scripted", "live"}
    assert {"scripted"} <= {load(p).source for p in TRACES}


@pytest.mark.parametrize("path", TRACES, ids=lambda p: p.stem)
def test_every_trace_replays_clean_with_all_evaluators_passing(path: Path, tmp_path: Path) -> None:
    conn, redis = fresh_world(tmp_path)
    res = replay_trace(load(path), conn, redis, tmp_path / "fb.jsonl")
    assert res.mismatches == [], res.mismatches
    assert res.violations == []
    assert [f for f in res.findings if not f.ok] == []
    assert len(res.findings) >= 5


@pytest.mark.parametrize("path", TRACES, ids=lambda p: p.stem)
def test_committed_traces_hold_no_run_specific_ids_or_personal_data(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"OPT-[0-9A-F]{12}|APT-H-[0-9A-F]{10}|ESC-[0-9A-F]{8}", text)
    assert not re.search(r"\+\d[\d -]{9,}|\b\d{10,}\b|@[\w-]+\.\w+|sk-[A-Za-z0-9]{8,}", text)


def test_replay_never_touches_the_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*a: object, **k: object) -> None:
        raise AssertionError("replay attempted an HTTP call")

    monkeypatch.setattr(httpx.Client, "__init__", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    conn, redis = fresh_world(tmp_path)
    assert replay_trace(load(TRACES[0]), conn, redis, tmp_path / "fb.jsonl").ok


# --- replay catches regressions in the deterministic layers ---------------------------------------------
def test_replay_detects_a_changed_tool_result(tmp_path: Path) -> None:
    trace = load(ROOT / "tests" / "traces" / "clarify-options-hold-confirm.json")
    raw = trace.model_dump_json().replace('"12:00"', '"11:55"', 1)
    conn, redis = fresh_world(tmp_path)
    res = replay_trace(Trace.model_validate_json(raw), conn, redis, tmp_path / "fb.jsonl")
    assert not res.ok and any(m.field in ("tool_events", "reply_text") for m in res.mismatches)


def test_replay_detects_a_change_in_the_ranking_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from setuhaul import tools

    real = tools.rank_options
    monkeypatch.setattr(tools, "rank_options", lambda decisions, limit=3: [
        o.model_copy(update={"rank": i}) for i, o in enumerate(reversed(real(decisions, limit)), start=1)])
    conn, redis = fresh_world(tmp_path)
    res = replay_trace(load(ROOT / "tests" / "traces" / "clarify-options-hold-confirm.json"), conn, redis, tmp_path / "fb.jsonl")
    assert not res.ok and res.mismatches


def test_replay_detects_a_change_in_the_checkpoint_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from setuhaul import tools

    monkeypatch.setattr(tools, "_quote_present", lambda ctx, quote: False)  # nobody's words count as approval any more
    conn, redis = fresh_world(tmp_path)
    res = replay_trace(load(ROOT / "tests" / "traces" / "never-mind-release.json"), conn, redis, tmp_path / "fb.jsonl")
    assert not res.ok and any(m.field in ("tool_events", "reply_text") for m in res.mismatches)


# --- sanitising ---------------------------------------------------------------------------------------------
def test_sanitiser_swaps_ids_for_stable_placeholders_and_redacts_personal_data() -> None:
    t = mk_trace(mk_turn(text="call me on +91-9000010006 or mail a.b@example.com", reply="Reference APT-H-ABCDEF0123 and OPT-0123456789AB; case ESC-DEADBEEF; key sk-abcdef123456",
                         events=[ev("search_slot_options", result={"option_set_id": "OPT-0123456789AB"})]))
    s = sanitize_trace(t)
    text = s.model_dump_json()
    assert "$HOLD1" in text and "$OPT1" in text and "$CASE1" in text
    assert "9000010006" not in text and "example.com" not in text and "sk-abcdef" not in text
    assert "[PHONE]" in text and "[EMAIL]" in text and "[SECRET]" in text
    assert sanitize_trace(s) == s  # idempotent
    assert s.clock_start == "2026-08-04T09:35:00+05:30"  # dates are not mistaken for phone numbers


# --- evaluators can fail --------------------------------------------------------------------------------------
HELD = {"status": "HELD"}


def test_identity_attempt_must_be_rejected() -> None:
    bad = mk_trace(mk_turn(events=[ev("search_slot_options", {"driver_id": "DRV007"}, {"status": "OK"})]))
    good = mk_trace(mk_turn(events=[ev("search_slot_options", {"driver_id": "DRV007"}, {"error": "INVALID_ARGUMENTS"}, executed=False)]))
    assert not all(f.ok for f in eval_identity(bad)) and all(f.ok for f in eval_identity(good))


def test_hold_without_options_and_confirm_in_same_message_are_flagged() -> None:
    no_options = mk_trace(mk_turn(events=[ev("request_hold", {"driver_quote": "hello"}, HELD)]))
    same_turn = mk_trace(mk_turn(events=[ev("search_slot_options", result={"option_set_id": "$OPT1"}),
                                         ev("request_hold", {"driver_quote": "hello"}, HELD),
                                         ev("confirm_reschedule", {"driver_quote": "hello"}, {"status": "CONFIRMED"})]))
    assert not all(f.ok for f in eval_tool_order(no_options))
    assert not all(f.ok for f in eval_tool_order(same_turn))


def test_committed_write_needs_the_drivers_own_words() -> None:
    bad = mk_trace(mk_turn(text="what are my options?", events=[ev("request_hold", {"driver_quote": "yes hold it"}, HELD)]))
    good = mk_trace(mk_turn(text="Yes   HOLD it please", events=[ev("request_hold", {"driver_quote": "yes hold it"}, HELD)]))
    assert not all(f.ok for f in eval_explicit_approval(bad)) and all(f.ok for f in eval_explicit_approval(good))


def test_unknown_option_set_is_flagged() -> None:
    bad = mk_trace(mk_turn(events=[ev("request_hold", {"option_set_id": "$OPT9"}, HELD)]))
    good = mk_trace(mk_turn(events=[ev("search_slot_options", result={"option_set_id": "$OPT1"}),
                                    ev("request_hold", {"option_set_id": "$OPT1"}, HELD)]))
    assert not all(f.ok for f in eval_option_set_ownership(bad)) and all(f.ok for f in eval_option_set_ownership(good))


def test_unbacked_booking_claims_are_flagged() -> None:
    assert not all(f.ok for f in eval_state_backed_wording(mk_trace(mk_turn(reply="Your appointment is confirmed for 12:00."))))
    assert not all(f.ok for f in eval_state_backed_wording(mk_trace(mk_turn(reply="I have held the 12:00 slot for you."))))
    assert all(f.ok for f in eval_state_backed_wording(mk_trace(mk_turn(reply="Your appointment is confirmed.", verified_confirmed=True))))


def test_round_cap_is_checked() -> None:
    many = [ev("get_shipment_status", result={"status": "x"}) for _ in range(5)]
    assert not all(f.ok for f in eval_round_caps(mk_trace(mk_turn(events=many))))
    seven = TurnTrace(turn=1, message_id="m", driver_text="x", rounds=[RoundTrace() for _ in range(7)], reply_text="x", reply_status="REPLIED")
    assert not all(f.ok for f in eval_round_caps(mk_trace(seven)))


def test_handoff_must_carry_its_context() -> None:
    thin = mk_trace(mk_turn(reply_status="HANDOFF", escalation_reason="NO_SLOT", handoff_context_keys=["language"]))
    full = mk_trace(mk_turn(reply_status="HANDOFF", escalation_reason="NO_SLOT",
                            handoff_context_keys=["language", "tools_executed", "recent_turns", "selected_shipment_id"]))
    assert not all(f.ok for f in eval_handoff_complete(thin)) and all(f.ok for f in eval_handoff_complete(full))


def test_run_evaluators_covers_every_evaluator_on_a_real_trace() -> None:
    findings = run_evaluators(load(ROOT / "tests" / "traces" / "clarify-options-hold-confirm.json"))
    assert {f.evaluator for f in findings} == {"identity", "tool_order", "explicit_approval", "option_set_ownership",
                                               "state_backed_wording", "round_caps", "handoff_complete"}


# --- failure taxonomy ----------------------------------------------------------------------------------------
@pytest.mark.parametrize(("turn", "expected"), [
    (mk_turn(reply_status="TEMPORARILY_UNAVAILABLE", escalation_reason="MODEL_UNAVAILABLE"), "DEPENDENCY"),
    (mk_turn(reply_status="LANGUAGE_PROMPT"), "INPUT"),
    (mk_turn(events=[ev("confirm_reschedule", result={"error": "TOOL_NOT_ALLOWED"}, executed=False)]), "TRAJECTORY"),
    (mk_turn(events=[ev("request_hold", result={"error": "APPROVAL_NOT_PRESENT"}, executed=False)]), "DECISION"),
    (mk_turn(events=[ev("compute_eta", result={"error": "INVALID_ARGUMENTS"}, executed=False)]), "TECHNICAL_OUTCOME"),
    (mk_turn(reply=""), "INSUFFICIENT_EVIDENCE"),
])
def test_failures_are_grouped(turn: TurnTrace, expected: str) -> None:
    assert expected in classify_failure(turn)


def test_a_clean_turn_has_no_failure_class() -> None:
    assert classify_failure(mk_turn()) == []


# --- recording ----------------------------------------------------------------------------------------------
def test_recording_gateway_keeps_calls_and_errors() -> None:
    rec = RecordingGateway(ScriptedGateway([ModelResponse(text="a"), PermanentModelError("x")]))
    rec.complete([], [{"name": "t", "description": "", "input_schema": {}}], POLICY)
    with pytest.raises(PermanentModelError):
        rec.complete([], [], POLICY)
    calls = rec.drain()
    assert calls[0].response is not None and calls[0].offered == ("t",) and calls[1].error == "PermanentModelError"
    assert rec.drain() == []


# --- the live run is blocked without an approved model --------------------------------------------------------
def test_live_scenario_refuses_to_run_without_an_approved_model(tmp_path: Path) -> None:
    import os

    env = {k: v for k, v in os.environ.items() if not k.startswith(("SETUHAUL_MODEL", "ANTHROPIC"))}
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_live_model_scenario.py")], env=env, capture_output=True,
                       text=True, cwd=tmp_path, check=False)
    assert r.returncode == 3 and "BLOCKED" in r.stderr and "Nothing was called" in r.stderr
    assert not (ROOT / "live_traces").exists() or not list((ROOT / "live_traces").glob("*.raw.json"))


def test_trace_files_round_trip_through_json() -> None:
    for p in TRACES:
        t = load(p)
        assert Trace.model_validate(json.loads(t.model_dump_json())) == t
