"""Delivery survives bad rows and locks, never announces old news, and the sweep keeps running."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from setuhaul.auth import telegram
from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection, parse_operational_timestamp
from setuhaul.jobs import send_driver_notifications as sender
from setuhaul.jobs.expire_holds import MaintenanceRunner
from tests.test_auto_hold import LATE, Chat
from tests.test_facility_follow_up import _approve
from tests.test_telegram_delivery import _bind, _queue, _row


def _db(tmp_path: Path, clock: FakeClock) -> Any:
    conn = open_connection(path=make_working_db(tmp_path / "x.sqlite3"))
    _bind(conn, clock, 111, "9000010006")
    return conn


@pytest.mark.parametrize("bad", ["not a time", "2026-08-04T09:30:00"])
def test_a_notice_with_a_bad_timestamp_does_not_block_the_ones_behind_it(tmp_path: Path, clock: FakeClock, bad: str) -> None:
    conn = _db(tmp_path, clock)
    first = _queue(conn, "DRV006", "bad", {"text": "bad"})
    second = _queue(conn, "DRV006", "good", {"text": "good"})
    conn.execute("update notification_outbox set created_at=? where outbox_id=?", (bad, first))
    conn.commit()
    fake = telegram.FakeTelegramClient()
    sender.send_driver_notifications(conn, clock, fake)
    assert [t for _, t, _ in fake.sent] == ["good"] and _row(conn, second)[0] == "SENT"
    assert _row(conn, first) == ("PERMANENT_FAILURE", "BAD_TIMESTAMP")


def test_a_locked_database_after_sending_does_not_send_twice(tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _db(tmp_path, clock)
    oid = _queue(conn, "DRV006", "k", {"text": "once"})
    real, failures = sender._mark, [0]

    def locked_twice(*args: Any) -> None:
        if failures[0] < 2:
            failures[0] += 1
            raise sqlite3.OperationalError("database is locked")
        real(*args)

    monkeypatch.setattr(sender, "_mark", locked_twice)
    fake = telegram.FakeTelegramClient()
    sender.send_driver_notifications(conn, clock, fake)
    sender.send_driver_notifications(conn, clock, fake)
    assert [t for _, t, _ in fake.sent] == ["once"] and _row(conn, oid)[0] == "SENT"


def _ages(chat: Chat, after_id: int) -> list[timedelta]:
    rows = chat.rows(f"select created_at from notification_outbox where recipient_type='DRIVER' and outbox_id > {after_id}")
    return [chat.deps.clock.now() - parse_operational_timestamp(r[0]) for r in rows]


def _last_id(chat: Chat) -> int:
    return int(chat.rows("select coalesce(max(outbox_id),0) from notification_outbox")[0][0])


def _sweep(chat: Chat) -> None:
    conn = open_connection(path=chat.deps.db_path)
    try:
        MaintenanceRunner(conn, chat.deps.clock).run_once()
    finally:
        conn.close()


@pytest.mark.parametrize("what", ["hold", "move"])
def test_what_lapsed_during_downtime_is_queued_as_old_news(tmp_path: Path, what: str) -> None:
    chat = Chat(tmp_path)
    if what == "hold":
        chat.say(LATE)
    else:
        _approve(chat, "Gurugram")
    before = _last_id(chat)
    chat.deps.clock.advance(timedelta(days=3))
    _sweep(chat)
    ages = _ages(chat, before)
    # Queued, but dated when it happened, so the sender skips it instead of announcing it now.
    assert ages and all(a > sender.NOTICE_MAX_AGE for a in ages)


def test_the_sweep_keeps_running_after_an_error(
        tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR)
    path = make_working_db(tmp_path / "s.sqlite3")
    calls: list[int] = []
    third = threading.Event()

    def flaky(self: MaintenanceRunner) -> None:
        calls.append(1)
        if len(calls) >= 3:
            third.set()
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(MaintenanceRunner, "run_once", flaky)
    runner = MaintenanceRunner(None, clock, 0.01, conn_factory=lambda: open_connection(path=path))
    runner.start()  # the sweep at start-up fails too, and must not stop the server starting
    try:
        assert third.wait(5)
    finally:
        runner.stop()
    assert "OperationalError" in caplog.text
