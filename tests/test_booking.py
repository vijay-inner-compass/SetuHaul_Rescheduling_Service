"""Phase 6: atomic confirmation with exact idempotency semantics."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from setuhaul import booking
from setuhaul.booking import (
    ConfirmCommand,
    OutcomeClass,
    acquire_hold,
    confirm_reschedule,
    render_confirmation_text,
)
from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection
from setuhaul.identity import AuthError
from tests.helpers import seed_everything_known, show_options

TTL = timedelta(seconds=180)
TABLES = ("appointments", "appointment_hold_meta", "appointment_events", "notification_outbox", "idempotency_keys")


@pytest.fixture()
def world(db: sqlite3.Connection, clock: FakeClock):
    seed_everything_known(db)
    opts = show_options(db, clock)
    held = acquire_hold(db, "DRV006", "THR001", "SHP1006", opts.option_set_id, 1, clock)
    assert held.status == "HELD"
    return db, clock, opts, held


def cmd(held, message_id="MSG-A", **kw) -> ConfirmCommand:  # type: ignore[no-untyped-def]
    base = {"driver_id": "DRV006", "thread_id": "THR001", "message_id": message_id, "shipment_id": "SHP1006",
                "hold_appointment_id": held.appointment_id}
    return ConfirmCommand(**{**base, **kw})


def snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple]]:  # type: ignore[type-arg]
    return {t: [tuple(r) for r in conn.execute(f"select * from {t} order by 1, 2")] for t in TABLES}


# --- the happy path ----------------------------------------------------------------------------
def test_confirmation_is_one_transaction_that_swaps_the_appointments(world) -> None:
    db, clock, _opts, held = world
    assert db.execute("select appointment_status from appointments where appointment_id='APT1006'").fetchone()[0] == "CONFIRMED"
    r = confirm_reschedule(db, cmd(held), clock)
    assert r.status == "CONFIRMED" and r.outcome is OutcomeClass.SUCCESS and r.replayed is False
    new = db.execute("select * from appointments where appointment_id=?", (held.appointment_id,)).fetchone()
    assert (new["appointment_status"], new["is_current"], new["replaced_appointment_id"]) == ("CONFIRMED", 1, "APT1006")
    assert new["confirmed_at"] is not None
    old = db.execute("select * from appointments where appointment_id='APT1006'").fetchone()
    assert (old["appointment_status"], old["is_current"], old["cancellation_reason"]) == ("CANCELLED", 0, "RESCHEDULED")
    meta = db.execute("select release_reason from appointment_hold_meta where appointment_id=?", (held.appointment_id,)).fetchone()
    assert meta[0] == "CONFIRMED"


def test_original_slot_becomes_available_only_after_commit(world) -> None:
    db, clock, _opts, held = world
    avail = lambda: db.execute("select availability_status from v_slot_availability where slot_id='SLOT-JAI-045'").fetchone()[0]
    assert avail() == "OCCUPIED"
    confirm_reschedule(db, cmd(held), clock)
    assert avail() == "AVAILABLE"


def test_history_and_three_notifications_are_written_with_the_booking(world) -> None:
    db, clock, opts, held = world
    confirm_reschedule(db, cmd(held), clock)
    types = [r[0] for r in db.execute("select event_type from appointment_events order by event_id")]
    assert types == ["HOLD_ACQUIRED", "ORIGINAL_ENDED", "RESCHEDULE_CONFIRMED"]
    boxes = db.execute("select recipient_type, recipient_ref, status from notification_outbox order by outbox_id").fetchall()
    assert [b["recipient_type"] for b in boxes] == ["WAREHOUSE_PLANNER", "CONSIGNEE", "OPERATIONS"]
    assert boxes[0]["recipient_ref"] == "Neha Bansal" and boxes[1]["recipient_ref"] == "CareSupply Rajasthan"
    assert all(b["status"] == "PENDING" for b in boxes)
    payload = json.loads(db.execute("select payload_json from notification_outbox where recipient_type='OPERATIONS'").fetchone()[0])
    assert payload["from_slot_id"] == "SLOT-JAI-045" and payload["to_slot_id"] == opts.items[0].slot_id


def test_shipment_without_an_original_appointment_just_confirms(db: sqlite3.Connection, clock: FakeClock) -> None:
    seed_everything_known(db)
    opts = show_options(db, clock, "THR001", "SHP1021", "11:20")
    held = acquire_hold(db, "DRV006", "THR001", "SHP1021", opts.option_set_id, 1, clock)
    r = confirm_reschedule(db, cmd(held, shipment_id="SHP1021"), clock)
    assert r.status == "CONFIRMED" and r.original_appointment_id is None
    types = [x[0] for x in db.execute("select event_type from appointment_events where shipment_id='SHP1021' order by event_id")]
    assert types == ["HOLD_ACQUIRED", "RESCHEDULE_CONFIRMED"]


# --- idempotency ---------------------------------------------------------------------------------
def test_exact_retry_returns_the_stored_result_and_writes_nothing(world) -> None:
    db, clock, _opts, held = world
    first = confirm_reschedule(db, cmd(held), clock)
    before = snapshot(db)
    again = confirm_reschedule(db, cmd(held), clock)
    assert again.status == "CONFIRMED" and again.replayed is True
    assert again.appointment_id == first.appointment_id
    assert snapshot(db) == before


def test_same_key_with_different_parameters_is_a_mismatch(world) -> None:
    db, clock, _opts, held = world
    confirm_reschedule(db, cmd(held), clock)
    before = snapshot(db)
    bad = confirm_reschedule(db, cmd(held, hold_appointment_id="APT-H-OTHER"), clock)
    assert bad.status == "IDEMPOTENCY_MISMATCH" and bad.outcome is OutcomeClass.BUSINESS_CONFLICT
    assert snapshot(db) == before


def test_idempotency_scope_includes_driver_thread_message_and_action(world) -> None:
    db, clock, _opts, held = world
    confirm_reschedule(db, cmd(held), clock)
    row = db.execute("select driver_id, thread_id, message_id, action from idempotency_keys").fetchone()
    assert tuple(row) == ("DRV006", "THR001", "MSG-A", "CONFIRM_RESCHEDULE")


def test_a_new_message_for_an_already_confirmed_hold_reports_it_without_new_writes(world) -> None:
    db, clock, _opts, held = world
    first = confirm_reschedule(db, cmd(held, "MSG-A"), clock)
    second = confirm_reschedule(db, cmd(held, "MSG-B"), clock)
    assert second.status == "ALREADY_CONFIRMED" and second.appointment_id == first.appointment_id
    assert db.execute("select count(*) from appointment_events where event_type='RESCHEDULE_CONFIRMED'").fetchone()[0] == 1


def test_response_loss_after_commit_is_recoverable_by_retry(world) -> None:
    db, clock, _opts, held = world
    confirm_reschedule(db, cmd(held), clock)  # driver never sees this reply
    retry = confirm_reschedule(db, cmd(held), clock)
    assert retry.status == "CONFIRMED" and retry.replayed
    assert db.execute("select count(*) from appointments where appointment_status='CONFIRMED' and shipment_id='SHP1006'").fetchone()[0] == 1


# --- rollback at every write point ---------------------------------------------------------------
@pytest.mark.parametrize("point", ["BEFORE_END_ORIGINAL", "AFTER_END_ORIGINAL", "AFTER_PROMOTE", "AFTER_META",
                                   "AFTER_EVENTS", "AFTER_OUTBOX", "AFTER_IDEMPOTENCY"])
def test_failure_at_any_write_point_rolls_everything_back(world, monkeypatch, point: str) -> None:
    db, clock, _opts, held = world
    before = snapshot(db)

    def boom(name: str) -> None:
        if name == point:
            raise RuntimeError(f"injected failure at {name}")

    monkeypatch.setattr(booking, "fault_hook", boom)
    r = confirm_reschedule(db, cmd(held), clock)
    assert r.status == "SYSTEM_ERROR" and r.outcome is OutcomeClass.SYSTEM_ERROR
    assert snapshot(db) == before
    assert db.execute("select appointment_status from appointments where appointment_id='APT1006'").fetchone()[0] == "CONFIRMED"
    monkeypatch.setattr(booking, "fault_hook", None)
    assert confirm_reschedule(db, cmd(held), clock).status == "CONFIRMED"  # retry succeeds afterwards


def test_database_outage_before_any_write_changes_nothing(working_db_path: Path, clock: FakeClock) -> None:
    db = open_connection(path=working_db_path)
    seed_everything_known(db)
    held = acquire_hold(db, "DRV006", "THR001", "SHP1006", show_options(db, clock).option_set_id, 1, clock)
    check = open_connection(path=working_db_path)
    before = snapshot(check)
    db.close()  # the connection dies
    r = confirm_reschedule(db, cmd(held), clock)
    assert r.status == "SYSTEM_ERROR" and r.outcome is OutcomeClass.SYSTEM_ERROR
    assert snapshot(check) == before
    check.close()


def test_lock_timeout_is_not_a_business_conflict(working_db_path: Path, clock: FakeClock) -> None:
    db = open_connection(path=working_db_path)
    seed_everything_known(db)
    held = acquire_hold(db, "DRV006", "THR001", "SHP1006", show_options(db, clock).option_set_id, 1, clock)
    db.execute("pragma busy_timeout=50")
    blocker = open_connection(path=working_db_path)
    blocker.execute("begin immediate")
    try:
        r = confirm_reschedule(db, cmd(held), clock)
    finally:
        blocker.rollback()
        blocker.close()
    assert r.status == "LOCK_TIMEOUT" and r.outcome is OutcomeClass.LOCK_TIMEOUT
    db.close()


# --- refusing what cannot be confirmed -----------------------------------------------------------
def test_expired_hold_cannot_be_confirmed_and_original_stands(world) -> None:
    db, clock, _opts, held = world
    clock.advance(TTL)
    r = confirm_reschedule(db, cmd(held), clock)
    assert r.status == "HOLD_EXPIRED" and r.outcome is OutcomeClass.BUSINESS_CONFLICT
    assert db.execute("select appointment_status from appointments where appointment_id=?", (held.appointment_id,)).fetchone()[0] == "CANCELLED"
    assert db.execute("select appointment_status, is_current from appointments where appointment_id='APT1006'").fetchone()[:] == ("CONFIRMED", 1)
    assert db.execute("select count(*) from notification_outbox where dedupe_key like 'HOLD_EXPIRED:%'").fetchone()[0] == 1


def test_released_hold_cannot_be_confirmed(world) -> None:
    from setuhaul.booking import release_hold

    db, clock, _opts, held = world
    release_hold(db, "DRV006", "THR001", held.appointment_id, "DRIVER_CANCELLED", clock)
    assert confirm_reschedule(db, cmd(held), clock).status == "HOLD_NOT_ACTIVE"


def test_feasibility_is_rechecked_and_a_lost_slot_releases_the_hold(world) -> None:
    db, clock, opts, held = world
    dock = opts.items[0].decision.dock_id
    db.execute("insert into dock_status_events (dock_event_id, dock_id, event_type, event_start_ts, event_end_ts, "
               "reason, created_at) values ('DEVT-Z',?, 'BREAKDOWN','2026-08-04T09:36:00+05:30',"
               "'2026-08-04T23:00:00+05:30','leveller','2026-08-04T09:36:00+05:30')", (dock,))
    db.commit()
    r = confirm_reschedule(db, cmd(held), clock)
    assert r.status == "FEASIBILITY_CHANGED" and "DOCK_EVENT_BLOCK" in r.reason_codes
    assert db.execute("select appointment_status from appointments where appointment_id=?", (held.appointment_id,)).fetchone()[0] == "CANCELLED"
    assert db.execute("select appointment_status from appointments where appointment_id='APT1006'").fetchone()[0] == "CONFIRMED"


def test_an_original_already_in_progress_is_never_ended(world) -> None:
    db, clock, _opts, held = world
    db.execute("update appointments set appointment_status='IN_PROGRESS' where appointment_id='APT1006'")
    db.commit()
    r = confirm_reschedule(db, cmd(held), clock)
    assert r.status == "ORIGINAL_NOT_RESCHEDULABLE"
    assert db.execute("select appointment_status from appointments where appointment_id='APT1006'").fetchone()[0] == "IN_PROGRESS"


def test_only_the_owner_of_the_hold_can_confirm(world) -> None:
    db, clock, _opts, held = world
    with pytest.raises(AuthError) as err:
        confirm_reschedule(db, cmd(held, driver_id="DRV007"), clock)
    assert err.value.code == "FOREIGN_HOLD"
    with pytest.raises(AuthError):
        confirm_reschedule(db, cmd(held, thread_id="THR002"), clock)
    assert db.execute("select count(*) from idempotency_keys").fetchone()[0] == 0


# --- confirmation wording comes from committed rows only -----------------------------------------
def test_confirmation_text_is_rendered_only_from_a_confirmed_row(world) -> None:
    db, clock, _opts, held = world
    with pytest.raises(ValueError, match="not confirmed"):
        render_confirmation_text(db, held.appointment_id)  # still only HELD
    confirm_reschedule(db, cmd(held), clock)
    text = render_confirmation_text(db, held.appointment_id)
    assert "confirmed" in text.lower() and "12:00" in text and "D2" in text
    with pytest.raises(ValueError):
        render_confirmation_text(db, "APT-NOPE")


# --- concurrent retries ---------------------------------------------------------------------------
def test_two_workers_confirming_the_same_message_yield_one_booking(tmp_path: Path, clock: FakeClock) -> None:
    path = make_working_db(tmp_path / "c.sqlite3")
    setup = open_connection(path=path)
    seed_everything_known(setup)
    held = acquire_hold(setup, "DRV006", "THR001", "SHP1006", show_options(setup, clock).option_set_id, 1, clock)
    setup.close()
    out: list[tuple[str, bool]] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        conn = open_connection(path=path)
        barrier.wait()
        r = confirm_reschedule(conn, cmd(held), clock)
        out.append((r.status, r.replayed))
        conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(out) == [("CONFIRMED", False), ("CONFIRMED", True)]
    check = open_connection(path=path)
    assert check.execute("select count(*) from appointment_events where event_type='RESCHEDULE_CONFIRMED'").fetchone()[0] == 1
    check.close()
