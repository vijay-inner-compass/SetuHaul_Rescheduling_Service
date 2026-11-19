"""Phase 6: cancellation and reversal are explicit workflows."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from setuhaul.booking import (
    ConfirmCommand,
    acquire_hold,
    confirm_reschedule,
    release_hold,
)
from setuhaul.cancellation import (
    cancel_confirmed_appointment,
    cancel_pending_change,
    request_original_slot_again,
)
from setuhaul.clock import FakeClock
from setuhaul.identity import AuthError
from setuhaul.jobs.expire_holds import expire_due_holds
from tests.helpers import at, eta_at, seed_consignee, seed_everything_known, show_options

TTL = timedelta(seconds=180)


@pytest.fixture()
def world(db: sqlite3.Connection, clock: FakeClock):
    seed_everything_known(db)
    return db, clock


def avail(db: sqlite3.Connection, slot_id: str) -> str:
    return str(db.execute("select availability_status from v_slot_availability where slot_id=?", (slot_id,)).fetchone()[0])


def state_of(db: sqlite3.Connection, appointment_id: str) -> tuple:  # type: ignore[type-arg]
    return tuple(db.execute("select appointment_status, is_current from appointments where appointment_id=?",
                            (appointment_id,)).fetchone())


# --- "never mind, keep my original slot" (before confirmation) ---------------------------------------
def test_never_mind_releases_the_hold_and_leaves_the_original_alone(world) -> None:
    db, clock = world
    opts = show_options(db, clock)
    held = acquire_hold(db, "DRV006", "THR001", "SHP1006", opts.option_set_id, 1, clock)
    assert avail(db, opts.items[0].slot_id) == "HELD"
    out = cancel_pending_change(db, "DRV006", "THR001", "SHP1006", clock)
    assert out.status == "RELEASED" and out.appointment_id == held.appointment_id
    assert state_of(db, "APT1006") == ("CONFIRMED", 1)
    assert avail(db, opts.items[0].slot_id) == "AVAILABLE"
    assert db.execute("select release_reason from appointment_hold_meta where appointment_id=?", (held.appointment_id,)).fetchone()[0] == "DRIVER_CANCELLED"


def test_never_mind_with_nothing_pending(world) -> None:
    db, clock = world
    assert cancel_pending_change(db, "DRV006", "THR001", "SHP1006", clock).status == "NOTHING_PENDING"


def test_never_mind_for_someone_elses_shipment_is_refused(world) -> None:
    db, clock = world
    with pytest.raises(AuthError) as err:
        cancel_pending_change(db, "DRV006", "THR001", "SHP1012", clock)
    assert err.value.code == "FOREIGN_SHIPMENT"


# --- cancelling a confirmed appointment ----------------------------------------------------------------
def test_cancel_before_cutoff_cancels_transactionally(world) -> None:
    db, clock = world  # APT1006 slot 10:00; now 09:35 -> 25 min away, so move the clock back
    clock.set(at("07:00"))
    out = cancel_confirmed_appointment(db, "DRV006", "THR001", "MSG-C1", "SHP1006", clock)
    assert out.status == "CANCELLED" and out.appointment_id == "APT1006"
    row = db.execute("select appointment_status, is_current, cancellation_reason from appointments where appointment_id='APT1006'").fetchone()
    assert tuple(row) == ("CANCELLED", 0, "DRIVER_CANCELLED")
    assert avail(db, "SLOT-JAI-045") == "AVAILABLE"
    assert [r[0] for r in db.execute("select event_type from appointment_events where appointment_id='APT1006'")] == ["APPOINTMENT_CANCELLED"]
    assert db.execute("select count(*) from notification_outbox").fetchone()[0] == 3


def test_cancel_exactly_at_cutoff_is_allowed_and_one_minute_inside_escalates(world) -> None:
    db, clock = world
    clock.set(at("08:00"))  # 120 minutes before the 10:00 slot
    assert cancel_confirmed_appointment(db, "DRV006", "THR001", "MSG-1", "SHP1006", clock).status == "CANCELLED"


def test_inside_cutoff_escalates_and_changes_nothing(world) -> None:
    db, clock = world
    clock.set(at("08:01"))
    out = cancel_confirmed_appointment(db, "DRV006", "THR001", "MSG-1", "SHP1006", clock)
    assert out.status == "ESCALATE" and out.escalation_code == "CANCELLATION_CUTOFF"
    assert state_of(db, "APT1006") == ("CONFIRMED", 1)
    assert avail(db, "SLOT-JAI-045") == "OCCUPIED"  # remains unavailable
    assert db.execute("select count(*) from appointment_events where event_type='APPOINTMENT_CANCELLED'").fetchone()[0] == 0


@pytest.mark.parametrize(("penalty", "exceptional"), [(1, 0), (0, 1)])
def test_customer_commitment_forces_escalation(world, penalty: int, exceptional: int) -> None:
    db, clock = world
    clock.set(at("07:00"))
    seed_consignee(db, "SHP1006", latest="12:00", penalty=penalty, exceptional=exceptional)
    out = cancel_confirmed_appointment(db, "DRV006", "THR001", "MSG-1", "SHP1006", clock)
    assert out.status == "ESCALATE" and out.escalation_code == "CUSTOMER_COMMITMENT"
    assert state_of(db, "APT1006") == ("CONFIRMED", 1)


def test_unknown_customer_commitment_is_not_assumed_harmless(db: sqlite3.Connection, clock: FakeClock) -> None:
    clock.set(at("07:00"))  # no consignee rows were seeded
    out = cancel_confirmed_appointment(db, "DRV006", "THR001", "MSG-1", "SHP1006", clock)
    assert out.status == "ESCALATE" and out.escalation_code == "CUSTOMER_COMMITMENT_UNKNOWN"


def test_cancel_is_idempotent_and_detects_mismatch(world) -> None:
    db, clock = world
    clock.set(at("07:00"))
    first = cancel_confirmed_appointment(db, "DRV006", "THR001", "MSG-1", "SHP1006", clock)
    again = cancel_confirmed_appointment(db, "DRV006", "THR001", "MSG-1", "SHP1006", clock)
    assert again.status == "CANCELLED" and again.replayed and again.appointment_id == first.appointment_id
    assert db.execute("select count(*) from appointment_events where event_type='APPOINTMENT_CANCELLED'").fetchone()[0] == 1
    # same key, different parameters: SHP1021 belongs to the same driver
    mismatch = cancel_confirmed_appointment(db, "DRV006", "THR001", "MSG-1", "SHP1021", clock)
    assert mismatch.status == "IDEMPOTENCY_MISMATCH"


def test_nothing_to_cancel_and_in_progress_cannot_be_cancelled(world) -> None:
    db, clock = world
    clock.set(at("07:00"))
    assert cancel_confirmed_appointment(db, "DRV006", "THR001", "MSG-1", "SHP1021", clock).status == "NOT_FOUND"
    db.execute("update appointments set appointment_status='IN_PROGRESS' where appointment_id='APT1006'")
    db.commit()
    assert cancel_confirmed_appointment(db, "DRV006", "THR001", "MSG-2", "SHP1006", clock).status == "NOT_CANCELLABLE"


def test_cancelling_also_drops_a_pending_hold(world) -> None:
    db, clock = world
    clock.set(at("07:00"))
    opts = show_options(db, clock)
    held = acquire_hold(db, "DRV006", "THR001", "SHP1006", opts.option_set_id, 1, clock)
    out = cancel_confirmed_appointment(db, "DRV006", "THR001", "MSG-1", "SHP1006", clock)
    assert out.status == "CANCELLED" and out.released_hold_id == held.appointment_id
    assert state_of(db, held.appointment_id)[0] == "CANCELLED"


def test_other_driver_cannot_cancel(world) -> None:
    db, clock = world
    clock.set(at("07:00"))
    with pytest.raises(AuthError):
        cancel_confirmed_appointment(db, "DRV007", "THR001", "MSG-1", "SHP1006", clock)


# --- reverting to the original slot after confirmation is a NEW request ---------------------------------
def confirm_move(db: sqlite3.Connection, clock: FakeClock, eta: str = "11:20") -> str:
    opts = show_options(db, clock, "THR001", "SHP1006", eta)
    held = acquire_hold(db, "DRV006", "THR001", "SHP1006", opts.option_set_id, 1, clock)
    r = confirm_reschedule(db, ConfirmCommand(driver_id="DRV006", thread_id="THR001", message_id="MSG-X",
                                              shipment_id="SHP1006", hold_appointment_id=held.appointment_id), clock)
    assert r.status == "CONFIRMED"
    return str(held.appointment_id)


def test_revert_to_original_is_a_fresh_option_not_an_automatic_restore(world) -> None:
    db, clock = world
    new_id = confirm_move(db, clock)
    before = state_of(db, new_id), state_of(db, "APT1006")
    out = request_original_slot_again(db, "DRV006", "THR001", "SHP1006", eta_at("09:50"), clock)
    assert out.status == "OPTION_AVAILABLE" and out.option_set is not None
    assert [i.slot_id for i in out.option_set.items] == ["SLOT-JAI-045"]
    assert (state_of(db, new_id), state_of(db, "APT1006")) == before  # nothing was changed or held
    assert db.execute("select count(*) from appointments where appointment_status='HELD'").fetchone()[0] == 0


def test_original_slot_taken_in_the_meantime_is_reported(world) -> None:
    db, clock = world
    confirm_move(db, clock)
    db.execute("insert into appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source, "
               "is_current, booked_at, updated_at) values ('EXT-1','SHP1018','SLOT-JAI-045','CONFIRMED','PLANNER',1,"
               "'2026-08-04T09:40:00+05:30','2026-08-04T09:40:00+05:30')")
    db.commit()
    out = request_original_slot_again(db, "DRV006", "THR001", "SHP1006", eta_at("09:50"), clock)
    assert out.status == "NO_LONGER_AVAILABLE" and "SLOT_OCCUPIED" in out.reason_codes and out.option_set is None


def test_original_slot_unusable_with_a_late_eta(world) -> None:
    db, clock = world
    confirm_move(db, clock)
    out = request_original_slot_again(db, "DRV006", "THR001", "SHP1006", eta_at("11:20"), clock)
    assert out.status == "NOT_FEASIBLE" and "RELEASE_AFTER_SLOT_START" in out.reason_codes


def test_no_previous_slot_to_return_to(world) -> None:
    db, clock = world
    out = request_original_slot_again(db, "DRV006", "THR001", "SHP1006", eta_at("09:50"), clock)
    assert out.status == "NO_PREVIOUS_SLOT"  # APT1006 is still the original; nothing was replaced


# --- when does released capacity return to the pool? ---------------------------------------------------
def test_when_each_released_slot_returns_to_the_pool(world) -> None:
    db, clock = world
    # 1. hold released by the driver: immediately
    o1 = show_options(db, clock)
    h1 = acquire_hold(db, "DRV006", "THR001", "SHP1006", o1.option_set_id, 1, clock)
    release_hold(db, "DRV006", "THR001", h1.appointment_id, "DRIVER_CANCELLED", clock)
    assert avail(db, o1.items[0].slot_id) == "AVAILABLE"
    # 2. hold expired: at the expiry instant, before any sweep and after it
    h2 = acquire_hold(db, "DRV006", "THR001", "SHP1006", show_options(db, clock).option_set_id, 1, clock)
    slot2 = h2.slot_id
    clock.advance(TTL)
    assert avail(db, str(slot2)) == "HELD"  # raw view still shows the unswept row...
    from setuhaul.capacity import build_facility_state

    assert str(slot2) not in build_facility_state(db, "FAC-JAI-01", clock.now()).occupancy  # ...but capacity ignores it
    expire_due_holds(db, clock)
    assert avail(db, str(slot2)) == "AVAILABLE"
    # 3. reschedule confirmed: the ORIGINAL slot returns only when the swap commits
    assert avail(db, "SLOT-JAI-045") == "OCCUPIED"
    confirm_move(db, clock)
    assert avail(db, "SLOT-JAI-045") == "AVAILABLE"
    # 4. a blocked slot stays unavailable even though its appointment ended
    db.execute("update appointment_slots set slot_status='BLOCKED' where slot_id='SLOT-JAI-045'")
    db.commit()
    assert avail(db, "SLOT-JAI-045") == "BLOCKED"


def test_cancelled_slot_returns_but_only_after_commit_and_not_when_escalated(world) -> None:
    db, clock = world
    clock.set(at("08:30"))
    cancel_confirmed_appointment(db, "DRV006", "THR001", "MSG-1", "SHP1006", clock)  # escalates
    assert avail(db, "SLOT-JAI-045") == "OCCUPIED"
    clock.set(at("07:00"))
    cancel_confirmed_appointment(db, "DRV006", "THR001", "MSG-2", "SHP1006", clock)
    assert avail(db, "SLOT-JAI-045") == "AVAILABLE"
