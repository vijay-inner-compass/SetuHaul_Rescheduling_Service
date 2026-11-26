"""The admin Model tab: figures from the recorded calls, report only."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection, parse_operational_timestamp
from setuhaul.model_report import (
    call_totals,
    message_totals,
    percentile,
    recent_calls,
    recent_turns,
)
from tests.auth_helpers import World, build_world

NOW = parse_operational_timestamp("2026-08-04T09:35:00+05:30")


@pytest.fixture()
def db(tmp_path: Path) -> Any:
    conn = open_connection(path=make_working_db(tmp_path / "m.sqlite3"))
    yield conn
    conn.close()


@pytest.fixture()
def world(tmp_path: Path) -> World:
    return build_world(tmp_path, FakeClock(NOW))


def _turn(conn: sqlite3.Connection, turn: str, age_s: float = 0, duration: float = 1000, status: str = "REPLIED") -> None:
    at = NOW - timedelta(seconds=age_s)
    conn.execute("insert into model_turns values (?,?,?,?,?,NULL)", (turn, at.isoformat(), at.timestamp(), duration, status))


def _call(conn: sqlite3.Connection, call: str, turn: str, age_s: float = 0, duration: float = 100, outcome: str = "OK",
          tokens: tuple[int, int] = (10, 5), cost: float | None = None, model: str = "m-1") -> None:
    at = NOW - timedelta(seconds=age_s)
    conn.execute("insert into model_calls values (?,?,?,?,'acme',?,?,?,?,?,?)",
                 (call, turn, at.isoformat(), at.timestamp(), model, duration, outcome, tokens[0], tokens[1], cost))


def test_percentile_nearest_rank() -> None:
    assert percentile([], 0.5) is None
    assert percentile([4, 1, 3, 2], 0.5) == 2 and percentile([4, 1, 3, 2], 0.95) == 4


def test_totals_from_known_rows(db: sqlite3.Connection) -> None:
    _turn(db, "T-old", age_s=100_000, duration=4000)
    _call(db, "C-old", "T-old", age_s=100_000, duration=400, tokens=(100, 50))
    _turn(db, "T-new", duration=2000)
    _call(db, "C1", "T-new", duration=100, cost=0.5)
    _call(db, "C2", "T-new", duration=200, outcome="TIMEOUT", tokens=(0, 0))
    _call(db, "C3", "T-new", duration=300)
    db.commit()
    day = call_totals(db, NOW.timestamp() - 86400)
    assert (day.calls, day.failed, day.p50_ms, day.p95_ms) == (3, 1, 200, 300)
    assert (day.input_tokens, day.output_tokens, day.cost_usd, day.unpriced) == (20, 10, 0.5, 1)
    every = call_totals(db, None)
    assert (every.calls, every.failed, every.p50_ms, every.p95_ms, every.unpriced) == (4, 1, 200, 400, 2)
    assert (every.input_tokens, every.output_tokens) == (120, 60)
    msg = message_totals(db)
    assert (msg.messages, msg.p50_ms, msg.p95_ms, msg.avg_calls) == (2, 2000, 4000, 2.0)
    assert (msg.avg_input_tokens, msg.avg_output_tokens, msg.avg_cost_usd) == (60.0, 30.0, 0.5)
    assert [r["turn_id"] for r in recent_turns(db)] == ["T-new", "T-old"]
    assert [r["call_id"] for r in recent_calls(db, limit=2)] == ["C3", "C2"]


def test_empty_totals(db: sqlite3.Connection) -> None:
    t, m = call_totals(db, None), message_totals(db)
    assert (t.calls, t.p50_ms, t.cost_usd, m.messages, m.avg_calls, m.avg_cost_usd) == (0, None, None, 0, 0.0, None)


def test_model_tab_empty_state(world: World) -> None:
    text = world.client("admin").get("/admin").text
    assert 'data-tab="model"' in text and "No model calls recorded yet." in text and "No model configured" in text
    for label in ("Call timeout", "Retries", "Cost budget per message", "LangSmith tracing", "price not set"):
        assert label in text, label


def _seed(world: World, **call: Any) -> None:
    conn = open_connection(path=world.path)
    _turn(conn, "T1")
    _call(conn, "C1", "T1", **call)
    conn.commit()
    conn.close()


def test_model_tab_shows_price_not_set_and_never_a_zero_cost(world: World) -> None:
    _seed(world)
    text = world.client("admin").get("/admin").text
    assert "price not set" in text and "$0" not in text
    assert "Last call OK at " in text and "0% of calls failed" in text and "m-1" in text


def test_admin_page_works_without_the_tables(world: World) -> None:
    conn = open_connection(path=world.path)
    conn.execute("drop table model_calls")
    conn.execute("drop table model_turns")
    conn.commit()
    conn.close()
    r = world.client("admin").get("/admin")
    assert r.status_code == 200 and "<h2>Users</h2>" in r.text
    assert "This database has no model records yet. Apply the migrations, then restart." in r.text


def test_model_names_are_escaped(world: World) -> None:
    _seed(world, model="<script>alert(1)</script>")
    text = world.client("admin").get("/admin").text
    assert "<script>alert(1)</script>" not in text and "&lt;script&gt;alert(1)&lt;/script&gt;" in text
