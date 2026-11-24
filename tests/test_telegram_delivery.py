"""Queued driver notifications go to the driver's bound Telegram chat."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from setuhaul.auth import telegram
from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection
from setuhaul.events import append_appointment_event, enqueue_notification
from setuhaul.jobs.send_driver_notifications import send_driver_notifications


def _queue(conn, ref: str, key: str, payload: dict | None = None, rtype: str = "DRIVER") -> int:  # type: ignore[no-untyped-def, type-arg]
    # Hang the notice on a real history event, as the booking code does.
    apt, ship = tuple(conn.execute("select appointment_id, shipment_id from appointments limit 1").fetchone())
    when = datetime.fromisoformat("2026-08-04T09:00:00+05:30")
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
