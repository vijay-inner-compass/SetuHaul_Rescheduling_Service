"""Phase 6: appointment changes reach downstream parties, outside the booking transaction."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from setuhaul import notifications
from setuhaul.booking import ConfirmCommand, acquire_hold, confirm_reschedule
from setuhaul.clock import FakeClock
from setuhaul.events import get_appointment_history
from setuhaul.jobs.deliver_notifications import run_delivery_once
from setuhaul.jobs.expire_holds import expire_due_holds
from setuhaul.notifications import (
    Notice,
    PermanentDeliveryError,
    deliver_pending_notifications,
    recipient_status,
)
from tests.helpers import seed_everything_known, show_options


@pytest.fixture()
def booked(db: sqlite3.Connection, clock: FakeClock):
    seed_everything_known(db)
    opts = show_options(db, clock)
    held = acquire_hold(db, "DRV006", "THR001", "SHP1006", opts.option_set_id, 1, clock)
    r = confirm_reschedule(db, ConfirmCommand(driver_id="DRV006", thread_id="THR001", message_id="M1",
                                              shipment_id="SHP1006", hold_appointment_id=held.appointment_id), clock)
    assert r.status == "CONFIRMED"
    return db, clock, held.appointment_id


class Recorder:
    """A receiver that drops repeats of the same dedupe key, like a well-behaved downstream."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.effective: set[str] = set()

    def __call__(self, n: Notice) -> None:
        self.calls.append(n.dedupe_key)
        self.effective.add(n.dedupe_key)


def test_notices_are_queued_with_the_booking_not_sent(booked) -> None:
    db, _clock, _ = booked
    rows = db.execute("select recipient_type, status, attempts from notification_outbox").fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [("WAREHOUSE_PLANNER", "PENDING", 0), ("CONSIGNEE", "PENDING", 0),
                                                  ("OPERATIONS", "PENDING", 0)]


def test_delivery_marks_each_recipient_sent_once(booked) -> None:
    db, clock, _ = booked
    rec = Recorder()
    out = run_delivery_once(db, clock, rec)
    assert (out.sent, out.failed, out.permanent_failures) == (3, 0, 0)
    assert len(rec.calls) == 3
    again = run_delivery_once(db, clock, rec)
    assert again.sent == 0 and len(rec.calls) == 3  # no resend of delivered notices
    event_id = db.execute("select event_id from appointment_events where event_type='RESCHEDULE_CONFIRMED'").fetchone()[0]
    assert [s for *_, s in recipient_status(db, event_id)] == ["SENT", "SENT", "SENT"]
    assert db.execute("select count(*) from notification_outbox where delivered_at is not null").fetchone()[0] == 3


def test_transient_failure_retries_then_succeeds(booked) -> None:
    db, clock, _ = booked
    state = {"fail": True}

    def flaky(n: Notice) -> None:
        if n.recipient_type == "CONSIGNEE" and state["fail"]:
            raise ConnectionError("smtp down")

    first = deliver_pending_notifications(db, clock, flaky)
    assert (first.sent, first.failed) == (2, 1)
    row = db.execute("select status, attempts, last_error from notification_outbox where recipient_type='CONSIGNEE'").fetchone()
    assert tuple(row) == ("FAILED", 1, "smtp down")
    state["fail"] = False
    second = deliver_pending_notifications(db, clock, flaky)
    assert second.sent == 1
    assert db.execute("select status, attempts from notification_outbox where recipient_type='CONSIGNEE'").fetchone()[:] == ("SENT", 2)


def test_permanent_failure_is_not_retried(booked) -> None:
    db, clock, _ = booked

    def reject(n: Notice) -> None:
        if n.recipient_type == "CONSIGNEE":
            raise PermanentDeliveryError("mailbox does not exist")

    out = deliver_pending_notifications(db, clock, reject)
    assert out.permanent_failures == 1 and out.sent == 2
    calls: list[str] = []
    deliver_pending_notifications(db, clock, lambda n: calls.append(n.recipient_type))
    assert calls == []  # PERMANENT_FAILURE is terminal
    assert db.execute("select status from notification_outbox where recipient_type='CONSIGNEE'").fetchone()[0] == "PERMANENT_FAILURE"


def test_transient_failure_becomes_permanent_after_max_attempts(booked) -> None:
    db, clock, _ = booked

    def down(n: Notice) -> None:
        raise TimeoutError("timeout")

    for _ in range(3):
        deliver_pending_notifications(db, clock, down)
    assert {r[0] for r in db.execute("select status from notification_outbox")} == {"PERMANENT_FAILURE"}
    assert deliver_pending_notifications(db, clock, down).failed == 0


def test_notification_failure_never_rolls_back_the_appointment(booked) -> None:
    db, clock, new_id = booked

    def down(n: Notice) -> None:
        raise ConnectionError("everything is down")

    deliver_pending_notifications(db, clock, down)
    assert db.execute("select appointment_status, is_current from appointments where appointment_id=?", (new_id,)).fetchone()[:] == ("CONFIRMED", 1)
    assert db.execute("select appointment_status from appointments where appointment_id='APT1006'").fetchone()[0] == "CANCELLED"


def test_duplicate_delivery_after_a_crash_is_absorbed_by_the_dedupe_key(booked, monkeypatch) -> None:
    db, clock, _ = booked
    rec = Recorder()
    real_mark = notifications._mark
    state = {"crash": True}

    def crashy(conn, outbox_id, status, attempts, error, now):  # type: ignore[no-untyped-def]
        if state["crash"] and status == "SENT":
            state["crash"] = False
            raise RuntimeError("process died after sending, before recording")
        return real_mark(conn, outbox_id, status, attempts, error, now)

    monkeypatch.setattr(notifications, "_mark", crashy)
    with pytest.raises(RuntimeError):
        deliver_pending_notifications(db, clock, rec)
    deliver_pending_notifications(db, clock, rec)  # restart: the unmarked notice is sent again
    assert len(rec.calls) == 4 and len(rec.effective) == 3  # one repeat, absorbed by the receiver
    assert {r[0] for r in db.execute("select status from notification_outbox")} == {"SENT"}


def test_enqueue_is_deduplicated(booked) -> None:
    db, clock, new_id = booked
    event_id = db.execute("select event_id from appointment_events where event_type='RESCHEDULE_CONFIRMED'").fetchone()[0]
    again = notifications.enqueue_downstream(db, event_id, "RESCHEDULE_CONFIRMED", "SHP1006", new_id, {}, clock.now())
    db.commit()
    assert again == 0 and db.execute("select count(*) from notification_outbox").fetchone()[0] == 3


def test_driver_expiry_notice_goes_through_the_same_delivery_path(db: sqlite3.Connection, clock: FakeClock) -> None:
    seed_everything_known(db)
    acquire_hold(db, "DRV006", "THR001", "SHP1006", show_options(db, clock).option_set_id, 1, clock)
    clock.advance(timedelta(seconds=180))
    expire_due_holds(db, clock)
    seen: list[Notice] = []
    run_delivery_once(db, clock, seen.append)
    assert [(n.recipient_type, n.recipient_ref) for n in seen] == [("DRIVER", "DRV006")]
    assert "lapsed" in seen[0].payload["text"]


def test_appointment_history_shows_who_changed_what_and_why(booked) -> None:
    db, _clock, new_id = booked
    history = get_appointment_history(db, "SHP1006")
    assert [(h["event_type"], h["appointment_id"]) for h in history] == [
        ("HOLD_ACQUIRED", new_id), ("ORIGINAL_ENDED", "APT1006"), ("RESCHEDULE_CONFIRMED", new_id)]
    ended = history[1]
    assert (ended["actor_type"], ended["actor_id"], ended["reason"]) == ("DRIVER", "DRV006", "RESCHEDULED")
    assert (ended["from_status"], ended["to_status"]) == ("CONFIRMED", "CANCELLED")
    assert history[2]["details"]["from_slot_id"] == "SLOT-JAI-045"


def test_vacant_contact_falls_back_to_the_role_name(db: sqlite3.Connection, clock: FakeClock) -> None:
    seed_everything_known(db)
    db.execute("update facility_contacts set active_flag=0 where facility_id='FAC-JAI-01' and contact_role='SHIFT_MANAGER'")
    db.commit()
    opts = show_options(db, clock)
    held = acquire_hold(db, "DRV006", "THR001", "SHP1006", opts.option_set_id, 1, clock)
    confirm_reschedule(db, ConfirmCommand(driver_id="DRV006", thread_id="THR001", message_id="M1",
                                          shipment_id="SHP1006", hold_appointment_id=held.appointment_id), clock)
    ops = db.execute("select recipient_ref from notification_outbox where recipient_type='OPERATIONS'").fetchone()[0]
    assert ops == "ROLE:SHIFT_MANAGER"
