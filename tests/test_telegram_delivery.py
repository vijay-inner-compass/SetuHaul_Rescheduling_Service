"""Queued driver notifications go to the driver's bound Telegram chat."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from setuhaul.auth import telegram
from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection
from setuhaul.events import append_appointment_event, enqueue_notification
from setuhaul.jobs.send_driver_notifications import DriverNoticeRunner, send_driver_notifications


def _queue(conn, ref: str, key: str, payload: dict | None = None, rtype: str = "DRIVER", when: datetime | None = None) -> int:  # type: ignore[no-untyped-def, type-arg]
    # Hang the notice on a real history event, as the booking code does.
    apt, ship = tuple(conn.execute("select appointment_id, shipment_id from appointments limit 1").fetchone())
    # Fresh by default: the tests' clock reads about 09:35.
    when = when or datetime.fromisoformat("2026-08-04T09:30:00+05:30")
    ev = append_appointment_event(conn, apt, ship, "NOTE", from_status=None, to_status=None, actor_type="SYSTEM",
                                  actor_id=None, reason=None, occurred_at=when)
    enqueue_notification(conn, key, rtype, ref, ev, payload or {"kind": "SLOT_CHANGED", "text": "Slot moved to 10:00"}, when)
    conn.commit()
    return int(conn.execute("select outbox_id from notification_outbox where dedupe_key=?", (key,)).fetchone()[0])


def test_driver_notice_goes_to_bound_chat(tmp_path: Path, clock: FakeClock) -> None:
    conn = open_connection(path=make_working_db(tmp_path / "n.sqlite3"))
    telegram.bind_from_contact(conn, telegram_user_id=111, chat_id=555, contact_user_id=111, contact_phone="+91 90000 10006", clock=clock)
    oid = _queue(conn, "DRV006", "k1")
    other = _queue(conn, "Some Planner", "k2", rtype="OPERATIONS")
    fake = telegram.FakeTelegramClient()
    result = send_driver_notifications(conn, clock, fake)
    assert result.sent == 1 and fake.sent == [(555, "Slot moved to 10:00", False)]
    status = {r["outbox_id"]: r["status"] for r in conn.execute("select outbox_id, status from notification_outbox")}
    assert status[oid] == "SENT" and status[other] == "PENDING"
    # A second pass sends nothing more.
    assert send_driver_notifications(conn, clock, fake).sent == 0 and len(fake.sent) == 1


def test_unbound_driver_notice_waits_and_failures_retry(tmp_path: Path, clock: FakeClock) -> None:
    conn = open_connection(path=make_working_db(tmp_path / "n2.sqlite3"))
    oid = _queue(conn, "DRV006", "k1")
    fake = telegram.FakeTelegramClient()
    # Not bound yet: nothing is sent and the notice stays queued for when they bind.
    assert send_driver_notifications(conn, clock, fake).sent == 0
    assert conn.execute("select status from notification_outbox where outbox_id=?", (oid,)).fetchone()[0] == "PENDING"
    telegram.bind_from_contact(conn, telegram_user_id=111, chat_id=555, contact_user_id=111, contact_phone="9000010006", clock=clock)

    class Boom:
        def send(self, chat_id: int, text: str, request_contact_button: bool = False) -> None:
            raise telegram.TelegramSendError("sendMessage failed")

    res = send_driver_notifications(conn, clock, Boom())
    assert res.sent == 0 and res.failed == 1
    row = conn.execute("select status, attempts, last_error from notification_outbox where outbox_id=?", (oid,)).fetchone()
    assert row["status"] == "FAILED" and row["attempts"] == 1 and "sendMessage failed" in row["last_error"]
    # The retry succeeds.
    assert send_driver_notifications(conn, clock, fake).sent == 1


def _bind(conn, clock, uid: int, phone: str) -> None:  # type: ignore[no-untyped-def]
    telegram.bind_from_contact(conn, telegram_user_id=uid, chat_id=uid + 1000, contact_user_id=uid, contact_phone=phone, clock=clock)


def test_unbound_backlog_does_not_starve_a_bound_driver(tmp_path: Path, clock: FakeClock) -> None:
    conn = open_connection(path=make_working_db(tmp_path / "n3.sqlite3"))
    for i in range(150):
        _queue(conn, "DRV007", f"u{i}")
    oid = _queue(conn, "DRV006", "bound")
    _bind(conn, clock, 111, "9000010006")
    fake = telegram.FakeTelegramClient()
    assert send_driver_notifications(conn, clock, fake, limit=100).sent == 1
    assert conn.execute("select status from notification_outbox where outbox_id=?", (oid,)).fetchone()[0] == "SENT"


def test_ineligible_driver_notice_stays_queued(tmp_path: Path, clock: FakeClock) -> None:
    conn = open_connection(path=make_working_db(tmp_path / "n4.sqlite3"))
    oid = _queue(conn, "DRV006", "k1")
    _bind(conn, clock, 111, "9000010006")
    conn.execute("update drivers set driver_status='SUSPENDED' where driver_id='DRV006'")
    conn.commit()
    fake = telegram.FakeTelegramClient()
    assert send_driver_notifications(conn, clock, fake).sent == 0 and fake.sent == []
    assert conn.execute("select status, attempts from notification_outbox where outbox_id=?", (oid,)).fetchone()[:] == ("PENDING", 0)


def test_malformed_notice_does_not_crash_the_batch(tmp_path: Path, clock: FakeClock) -> None:
    conn = open_connection(path=make_working_db(tmp_path / "n5.sqlite3"))
    bad = _queue(conn, "DRV006", "bad")
    good = _queue(conn, "DRV006", "good")
    conn.execute("update notification_outbox set payload_json='{not json' where outbox_id=?", (bad,))
    conn.commit()
    _bind(conn, clock, 111, "9000010006")
    fake = telegram.FakeTelegramClient()
    res = send_driver_notifications(conn, clock, fake)
    assert res.sent == 1 and res.permanent_failures == 1
    status = {r["outbox_id"]: r["status"] for r in conn.execute("select outbox_id, status from notification_outbox")}
    assert status[bad] == "PERMANENT_FAILURE" and status[good] == "SENT"


def test_ineligible_backlog_does_not_starve_an_eligible_driver(tmp_path: Path, clock: FakeClock) -> None:
    conn = open_connection(path=make_working_db(tmp_path / "n6.sqlite3"))
    _bind(conn, clock, 111, "9000010006")
    _bind(conn, clock, 222, "9000010007")
    # More old notices for a bound but suspended driver than one batch holds.
    for i in range(5):
        _queue(conn, "DRV007", f"s{i}")
    first = _queue(conn, "DRV006", "ok1")
    second = _queue(conn, "DRV006", "ok2")
    third = _queue(conn, "DRV006", "ok3")
    conn.execute("update drivers set driver_status='SUSPENDED' where driver_id='DRV007'")
    conn.commit()
    # The limit counts delivered work, not the notices skipped on the way to it.
    assert send_driver_notifications(conn, clock, telegram.FakeTelegramClient(), limit=2).sent == 2
    status = {r["outbox_id"]: r["status"] for r in conn.execute("select outbox_id, status from notification_outbox")}
    assert status[first] == "SENT" and status[second] == "SENT" and status[third] == "PENDING"


def _row(conn, oid: int) -> tuple:  # type: ignore[no-untyped-def, type-arg]
    return tuple(conn.execute("select status, last_error from notification_outbox where outbox_id=?", (oid,)).fetchone())


def test_a_notice_older_than_thirty_minutes_is_marked_and_not_sent(tmp_path: Path, clock: FakeClock) -> None:
    conn = open_connection(path=make_working_db(tmp_path / "o.sqlite3"))
    _bind(conn, clock, 111, "9000010006")
    old = _queue(conn, "DRV006", "old", {"text": "old"}, when=clock.now() - timedelta(minutes=31))
    new = _queue(conn, "DRV006", "new", {"text": "new"}, when=clock.now() - timedelta(minutes=29))
    fake = telegram.FakeTelegramClient()
    result = send_driver_notifications(conn, clock, fake)
    assert [t for _, t, _ in fake.sent] == ["new"] and result.sent == 1 and result.permanent_failures == 1
    assert _row(conn, old) == ("PERMANENT_FAILURE", "TOO_OLD") and _row(conn, new)[0] == "SENT"


def test_a_late_binding_driver_gets_no_old_backlog(tmp_path: Path, clock: FakeClock) -> None:
    conn = open_connection(path=make_working_db(tmp_path / "l.sqlite3"))
    _queue(conn, "DRV006", "k1", when=clock.now() - timedelta(hours=5))
    fake = telegram.FakeTelegramClient()
    assert send_driver_notifications(conn, clock, fake).sent == 0
    _bind(conn, clock, 111, "9000010006")
    assert send_driver_notifications(conn, clock, fake).sent == 0 and fake.sent == []


def _runner_db(tmp_path: Path, clock: FakeClock) -> Path:
    path = make_working_db(tmp_path / "r.sqlite3")
    conn = open_connection(path=path)
    _bind(conn, clock, 111, "9000010006")
    _queue(conn, "DRV006", "k1", {"text": "hello"})
    conn.close()
    return path


def test_a_failing_pass_is_logged_without_secrets_and_the_next_one_sends(
        tmp_path: Path, clock: FakeClock, caplog: pytest.LogCaptureFixture) -> None:
    path = _runner_db(tmp_path, clock)
    calls: list[str] = []

    class Flaky:
        def send(self, chat_id: int, text: str, request_contact_button: bool = False) -> None:
            calls.append(text)
            if len(calls) == 1:
                raise RuntimeError("https://api.telegram.org/botSECRET/sendMessage")

    runner = DriverNoticeRunner(clock, Flaky(), lambda: open_connection(path=path))
    assert runner.run_once() is None
    assert "RuntimeError" in caplog.text and "SECRET" not in caplog.text
    again = runner.run_once()
    assert again is not None and again.sent == 1


def test_the_runner_thread_sends_and_stops(tmp_path: Path, clock: FakeClock, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR)
    path = _runner_db(tmp_path, clock)
    sent = threading.Event()

    class Signal:
        def send(self, chat_id: int, text: str, request_contact_button: bool = False) -> None:
            sent.set()

    runner = DriverNoticeRunner(clock, Signal(), lambda: open_connection(path=path), interval_s=0.01)
    runner.start()
    try:
        assert sent.wait(5)
    finally:
        runner.stop()
    assert not any(t.name == "setuhaul-driver-notices" and t.is_alive() for t in threading.enumerate())
