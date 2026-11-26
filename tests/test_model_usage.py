"""Every model call and the driver message it belongs to are recorded, without ever hurting the message."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection, parse_operational_timestamp
from setuhaul.demo_ui import build_demo_app
from setuhaul.model_gateway import (
    ModelMessage,
    ModelPolicy,
    ModelResponse,
    ModelTimeout,
    ModelUnavailable,
    PermanentModelError,
    ScriptedGateway,
    TransientModelError,
    complete_with_retry,
)
from setuhaul.model_usage import TurnRecorder, describe_model
from setuhaul.stub_model import StubGateway
from tests.test_demo_driver import _driver

POLICY = ModelPolicy(provider="acme", model_id="m-1")


@pytest.fixture()
def db(tmp_path: Path) -> Any:
    conn = open_connection(path=make_working_db(tmp_path / "u.sqlite3"))
    yield conn
    conn.close()


def _recorder() -> TurnRecorder:
    return TurnRecorder(FakeClock(parse_operational_timestamp("2026-08-04T09:35:00+05:30")))


def _rows(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in conn.execute("select outcome, input_tokens, output_tokens, cost_usd from model_calls order by rowid")]


def _ask(recorder: TurnRecorder, steps: list[Any], policy: ModelPolicy = POLICY) -> ModelResponse:
    return complete_with_retry(recorder.wrap(ScriptedGateway(steps)), [], [], policy)


def test_a_retry_is_two_rows(db: sqlite3.Connection) -> None:
    rec = _recorder()
    _ask(rec, [ModelTimeout(), ModelResponse(text="x", input_tokens=10, output_tokens=5)])
    rec.save(db, "REPLIED", None)
    assert _rows(db) == [("TIMEOUT", 0, 0, None), ("OK", 10, 5, None)]
    turn = db.execute("select status, reason_code, duration_ms from model_turns").fetchall()
    assert len(turn) == 1 and turn[0]["status"] == "REPLIED" and turn[0]["duration_ms"] >= 0
    call = db.execute("select provider, model_id from model_calls limit 1").fetchone()
    assert tuple(call) == ("acme", "m-1")


@pytest.mark.parametrize(("error", "outcome"), [
    (TransientModelError("busy"), "TRANSIENT"),
    (PermanentModelError("no"), "REFUSED"),
    (ModelUnavailable("MODEL_NOT_CONFIGURED"), "NOT_CONFIGURED"),
    (ValueError("boom"), "ERROR"),
])
def test_each_error_has_an_outcome_and_is_re_raised(db: sqlite3.Connection, error: Exception, outcome: str) -> None:
    rec = _recorder()
    wrapped = rec.wrap(ScriptedGateway([error]))
    with pytest.raises(type(error)) as caught:
        wrapped.complete([], [], POLICY)
    assert caught.value is error
    rec.save(db, "HANDOFF", "MODEL_UNAVAILABLE")
    assert [r[0] for r in _rows(db)] == [outcome]


def test_cost_is_stored_only_when_priced(db: sqlite3.Connection) -> None:
    answer = ModelResponse(text="x", input_tokens=1_000_000, output_tokens=1_000_000)
    rec = _recorder()
    _ask(rec, [answer], ModelPolicy(provider="a", model_id="b", price_in_per_mtok=1.0, price_out_per_mtok=2.0))
    _ask(rec, [answer])
    rec.save(db, "REPLIED", None)
    assert [r[3] for r in _rows(db)] == [3.0, None]


def test_nothing_is_written_when_no_model_was_called(db: sqlite3.Connection) -> None:
    _recorder().save(db, "REPLIED", None)
    assert db.execute("select count(*) from model_turns").fetchone()[0] == 0


def test_save_failure_is_swallowed(tmp_path: Path) -> None:
    path = make_working_db(tmp_path / "ro.sqlite3")
    rec = _recorder()
    _ask(rec, [ModelResponse(text="x")])
    conn = open_connection(path=path, readonly=True)
    try:
        rec.save(conn, "REPLIED", None)
    finally:
        conn.close()


def test_the_stand_in_and_the_missing_model_are_named() -> None:
    from setuhaul.api import UnconfiguredGateway

    assert describe_model(StubGateway(), POLICY) == ("stand-in", "scripted stand-in")
    assert describe_model(UnconfiguredGateway(), POLICY) == ("", "")
    assert describe_model(ScriptedGateway([]), POLICY) == ("acme", "m-1")


def _send(tmp_path: Path) -> tuple[Any, Any, Any]:
    app, deps = build_demo_app(tmp_path, live_factory=StubGateway)
    return deps, _driver(app), {"text": "my truck is 60 minutes late"}


def test_a_demo_message_records_a_turn_and_its_calls(tmp_path: Path) -> None:
    deps, driver, body = _send(tmp_path)
    reply = driver.post("/demo/driver/api/message", json=body).json()
    conn = open_connection(path=deps.db_path)
    turns = [dict(r) for r in conn.execute("select * from model_turns")]
    calls = [dict(r) for r in conn.execute("select * from model_calls")]
    conn.close()
    assert len(turns) == 1 and turns[0]["status"] == reply["status"]
    assert calls and all(c["turn_id"] == turns[0]["turn_id"] and c["provider"] == "stand-in" for c in calls)
    stored = " ".join(str(v) for row in turns + calls for v in row.values())
    assert "late" not in stored and "DRV006" not in stored


def test_reply_survives_a_database_without_the_tables(tmp_path: Path) -> None:
    deps, driver, body = _send(tmp_path)
    conn = open_connection(path=deps.db_path)
    conn.execute("drop table model_calls")
    conn.execute("drop table model_turns")
    conn.commit()
    conn.close()
    reply = driver.post("/demo/driver/api/message", json=body).json()
    assert reply["http_status"] == 200 and "holding" in reply["reply"]


def test_a_failing_turn_is_recorded_and_still_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deps, driver, body = _send(tmp_path)

    def explode(message: Any, trusted: Any, clock: Any, services: Any) -> Any:
        services.gateway.complete([ModelMessage(role="user", content="hi")], [], services.policy)
        raise RuntimeError("part-way")

    monkeypatch.setattr("setuhaul.api.handle_message", explode)
    with pytest.raises(RuntimeError):
        driver.post("/demo/driver/api/message", json=body)
    conn = open_connection(path=deps.db_path)
    assert [r["status"] for r in conn.execute("select status from model_turns")] == ["ERROR"]
    assert conn.execute("select count(*) from model_calls").fetchone()[0] == 1
    conn.close()
