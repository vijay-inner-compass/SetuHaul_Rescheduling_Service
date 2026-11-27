"""When a dependency fails the driver still gets an answer, the hand-off still reaches a person, and the log says why."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
from pathlib import Path

import fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient

from setuhaul import booking, cancellation, gate
from setuhaul.api import AppDeps, create_app
from setuhaul.clock import FakeClock
from setuhaul.config import load_settings
from setuhaul.escalation import HandoffRequest, JsonlEscalationSink, list_open_cases
from setuhaul.identity import provision_identities
from setuhaul.jobs.expire_holds import MaintenanceRunner
from setuhaul.memory import ConversationMemoryStore
from setuhaul.model_gateway import (
    AnthropicAdapter,
    ModelMessage,
    ModelPolicy,
    OpenAIAdapter,
    PermanentModelError,
)
from setuhaul.observability import configure_logging
from setuhaul.stub_model import StubGateway
from tests.conv_helpers import Chat, say_text, tc
from tests.helpers import seed_everything_known

SEARCH = tc("search_slot_options", shipment_id="SHP1006", report={"kind": "late_by", "minutes": 60})


def _dead_redis() -> tuple[fakeredis.FakeServer, fakeredis.FakeRedis]:
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server, decode_responses=True)
    server.connected = False
    return server, client


# --- F04: Redis outage --------------------------------------------------------------------------------------------
def test_a_driver_message_is_still_handled_with_redis_down(db: sqlite3.Connection, working_db_path: Path, clock: FakeClock,
                                                          tmp_path: Path) -> None:
    seed_everything_known(db)
    provision_identities(db)
    _, redis = _dead_redis()
    deps = AppDeps(db_path=working_db_path, redis=redis, webhook_secret=b"s", coordinator_tokens={},
                   fallback_path=tmp_path / "f.jsonl", clock=clock, gateway_factory=StubGateway)
    body = json.dumps({"from": "+91 90000 10006", "message_id": "m1", "text": "SHP1006 is 60 minutes late"}).encode()
    r = TestClient(create_app(deps), raise_server_exceptions=False).post(
        "/v1/driver/messages", content=body, headers={"x-signature": hmac.new(b"s", body, hashlib.sha256).hexdigest()})
    assert r.status_code == 200 and "I am holding" in r.json()["reply"]


def test_what_was_written_during_an_outage_is_seen_after_it(db: sqlite3.Connection, clock: FakeClock) -> None:
    server = fakeredis.FakeServer()
    redis = fakeredis.FakeRedis(server=server, decode_responses=True)
    store = ConversationMemoryStore(db, redis, clock, load_settings())
    thread = store.open_thread("DRV006", "SHP1006")
    store.append("DRV006", thread, "DRIVER", "before")
    server.connected = False
    store.append("DRV006", thread, "DRIVER", "during")
    state = store.load("DRV006", thread).state
    store.save_structured_state("DRV006", thread, state.model_copy(update={"language": "hi"}))
    server.connected = True
    after = store.load("DRV006", thread)
    assert [t.text for t in after.turns] == ["before", "during"] and after.state.language == "hi"


# --- F05: a provider answer nobody expected -----------------------------------------------------------------------
ANSWERS = {
    "html": httpx.Response(200, text="<html>proxy</html>"),
    "anthropic tool call without an id": httpx.Response(200, json={"content": [{"type": "tool_use", "name": "x"}]}),
    "a list instead of an object": httpx.Response(200, json=[1, 2]),
}


@pytest.mark.parametrize("adapter", [AnthropicAdapter, OpenAIAdapter])
@pytest.mark.parametrize("label", ["html", "a list instead of an object"])
def test_an_unreadable_provider_answer_is_a_model_error(adapter, label: str) -> None:  # type: ignore[no-untyped-def]
    gateway = adapter(api_key="k", transport=httpx.MockTransport(lambda request: ANSWERS[label]))
    with pytest.raises(PermanentModelError):
        gateway.complete([ModelMessage(role="user", content="x")], [], ModelPolicy(model_id="m"))


def test_a_malformed_tool_call_is_a_model_error() -> None:
    gateway = AnthropicAdapter(api_key="k", transport=httpx.MockTransport(
        lambda request: ANSWERS["anthropic tool call without an id"]))
    with pytest.raises(PermanentModelError):
        gateway.complete([ModelMessage(role="user", content="x")], [], ModelPolicy(model_id="m"))


@pytest.fixture()
def chat(db: sqlite3.Connection, clock: FakeClock, tmp_path: Path) -> Chat:
    seed_everything_known(db)
    return Chat(db, clock, tmp_path)


def test_a_crash_in_the_model_step_still_answers_and_hands_off(chat: Chat) -> None:
    r = chat.say("SHP1006 is late", [KeyError("id")])
    assert r.status == "TEMPORARILY_UNAVAILABLE" and r.escalation_reason == "MODEL_UNAVAILABLE" and r.case_id


def test_a_crash_after_a_hold_reports_the_hold_not_that_nothing_happened(chat: Chat) -> None:
    r = chat.say("SHP1006 is 60 minutes late", [SEARCH, RuntimeError("boom")])
    assert r.text.startswith("I am holding") and "Nothing has been" not in r.text and chat.held() == 1


# --- F06: hand-offs written while the database was down -------------------------------------------------------------
def _fallback(path: Path, clock: FakeClock, thread: str = "THR001", reason: str = "DATABASE_UNAVAILABLE") -> None:
    JsonlEscalationSink(path).record(HandoffRequest(reason_code=reason, driver_id="DRV006", thread_id=thread,
                                                    shipment_id=None, summary="s", created_at=clock.now()))


def test_the_sweep_loads_fallback_handoffs_each_time_there_are_some(db: sqlite3.Connection, clock: FakeClock,
                                                                    tmp_path: Path) -> None:
    path = tmp_path / "fallback.jsonl"
    runner = MaintenanceRunner(db, clock, fallback_path=path)
    _fallback(path, clock)
    runner.run_once()
    assert len(list_open_cases(db)) == 1 and not path.exists()
    _fallback(path, clock, reason="SAFETY")  # a second outage
    runner.run_once()
    assert len(list_open_cases(db)) == 2 and not path.exists()


def test_a_broken_line_is_set_aside_and_the_rest_are_loaded(db: sqlite3.Connection, clock: FakeClock, tmp_path: Path,
                                                            caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "fallback.jsonl"
    path.write_text('{"reason_code": "SAF\n', encoding="utf-8")
    _fallback(path, clock)
    MaintenanceRunner(db, clock, fallback_path=path).run_once()
    assert len(list_open_cases(db)) == 1
    assert (tmp_path / "fallback.jsonl.rejected").read_text(encoding="utf-8").startswith('{"reason_code": "SAF')
    assert "could not be loaded" in caplog.text


def test_a_busy_database_keeps_the_handoffs_for_the_next_sweep(db: sqlite3.Connection, clock: FakeClock, tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    from setuhaul import escalation

    path = tmp_path / "fallback.jsonl"
    _fallback(path, clock)
    real = escalation.create_handoff

    def locked(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise sqlite3.OperationalError("database is locked")

    runner = MaintenanceRunner(db, clock, fallback_path=path)
    monkeypatch.setattr(escalation, "create_handoff", locked)
    runner.run_once()
    monkeypatch.setattr(escalation, "create_handoff", real)
    runner.run_once()
    assert len(list_open_cases(db)) == 1


# --- F11: failures can be seen --------------------------------------------------------------------------------------
def test_the_application_logs_info_events_once_configured() -> None:
    logger = logging.getLogger("setuhaul")
    before, level = list(logger.handlers), logger.level
    # Start without the service's handler, whatever an earlier test set up.
    logger.handlers = [h for h in before if not getattr(h, "_setuhaul", False)]
    logger.setLevel(logging.NOTSET)
    others = len(logger.handlers)
    try:
        configure_logging()
        configure_logging()
        assert logger.isEnabledFor(logging.INFO) and len(logger.handlers) == others + 1
    finally:
        logger.handlers, logger.level = before, level


def test_one_failing_sweep_step_does_not_stop_the_others(db: sqlite3.Connection, clock: FakeClock,
                                                         monkeypatch: pytest.MonkeyPatch,
                                                         caplog: pytest.LogCaptureFixture) -> None:
    def broken(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("token=SECRET")

    monkeypatch.setattr(gate, "sweep", broken)
    MaintenanceRunner(db, clock).run_once()
    assert db.execute("select count(*) from maintenance_runs").fetchone()[0] == 1  # the purge, which comes after
    assert "gate sweep" in caplog.text and "RuntimeError" in caplog.text and "SECRET" not in caplog.text


@pytest.mark.parametrize("module,say_first", [(booking, False), (cancellation, True)])
def test_a_swallowed_booking_failure_is_logged(chat: Chat, monkeypatch: pytest.MonkeyPatch,
                                               caplog: pytest.LogCaptureFixture, module, say_first: bool) -> None:  # type: ignore[no-untyped-def]
    def broken(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("token=SECRET")

    monkeypatch.setattr(module, "enqueue_downstream", broken)
    if say_first:
        chat.clock.advance(-(chat.clock.now() - chat.clock.now().replace(hour=6, minute=0)))
        chat.db.execute("update consignee_commitments set penalty_applies=0, exceptional=0")
        chat.db.commit()
        call = tc("cancel_appointment", shipment_id="SHP1006", driver_quote="cancel")
        chat.say("cancel my appointment", [call, say_text("x")])
        chat.say("yes", [tc("cancel_appointment", shipment_id="SHP1006", driver_quote="yes"), say_text("x")])
    else:
        chat.say("SHP1006 is 60 minutes late", [SEARCH, say_text("x")])
        from tests.conv_helpers import facts_from, tc_dyn

        chat.say("yes", [tc_dyn("confirm_reschedule", lambda m: {
            "hold_appointment_id": facts_from(m)["live_hold"]["appointment_id"], "driver_quote": "yes"}), say_text("x")])
    assert "RuntimeError" in caplog.text and "SECRET" not in caplog.text
