"""Phase 4: persisted option sets and reconciliation against live state."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from setuhaul.allocation import rank_options
from setuhaul.capacity import Occupant, build_facility_state
from setuhaul.clock import FakeClock
from setuhaul.feasibility import build_context, search_feasible_slots
from setuhaul.identity import AuthError
from setuhaul.option_history import (
    OPTION_TTL,
    OptionSet,
    diff_option_set,
    get_latest_option_set,
    get_option_set,
    purge_option_sets,
    reconcile_option_set,
    record_option_set,
)
from tests.helpers import at, eta_at, seed_everything_known, with_state


@pytest.fixture()
def world(db: sqlite3.Connection, clock: FakeClock):
    seed_everything_known(db)
    thread = "THR001"  # DRV006 / SHP1006 in the snapshot
    return db, clock, thread


def show(db: sqlite3.Connection, clock: FakeClock, thread: str = "THR001", shipment: str = "SHP1006") -> OptionSet:
    ctx = build_context(db, shipment, eta_at("11:20"), clock.now())
    state = build_facility_state(db, ctx.facility_id, clock.now())
    options = rank_options(search_feasible_slots(ctx, state).feasible, limit=3)
    return record_option_set(db, ctx, thread, options, state, clock)


# --- storing -----------------------------------------------------------------------------------
def test_option_set_records_everything_shown(world) -> None:
    db, clock, _ = world
    s = show(db, clock)
    assert [i.rank for i in s.items] == [1, 2, 3]
    assert s.shown_at == clock.now() and s.expires_at == clock.now() + OPTION_TTL
    assert all(i.slot_version and i.feasibility_hash and i.rules_version.startswith("R1-") for i in s.items)
    row = db.execute("select count(*) from option_set_items where option_set_id=?", (s.option_set_id,)).fetchone()
    assert row[0] == 3
    assert s.context.release_time == at("11:20")


def test_stored_set_round_trips_exactly(world) -> None:
    db, clock, _ = world
    s = show(db, clock)
    again = get_option_set(db, "DRV006", "THR001", "SHP1006", s.option_set_id)
    assert again == s


def test_latest_is_scoped_and_most_recent(world, clock: FakeClock) -> None:
    db, clock, _ = world
    first = show(db, clock)
    clock.advance(timedelta(minutes=2))
    second = show(db, clock)
    latest = get_latest_option_set(db, "DRV006", "THR001", "SHP1006")
    assert latest is not None and latest.option_set_id == second.option_set_id != first.option_set_id
    assert get_latest_option_set(db, "DRV006", "THR001", "SHP1021") is None
    assert get_latest_option_set(db, "DRV007", "THR001", "SHP1006") is None


# --- ownership -----------------------------------------------------------------------------------
def test_option_set_cannot_be_selected_by_another_driver_thread_or_shipment(world) -> None:
    db, clock, _ = world
    s = show(db, clock)
    for args in [("DRV007", "THR001", "SHP1006"), ("DRV006", "THR002", "SHP1006"), ("DRV006", "THR001", "SHP1021")]:
        with pytest.raises(AuthError) as err:
            get_option_set(db, *args, s.option_set_id)
        assert err.value.code == "FOREIGN_OPTION_SET"
    with pytest.raises(AuthError):
        get_option_set(db, "DRV006", "THR001", "SHP1006", "OPT-NOPE")


def test_cannot_record_a_set_for_a_foreign_thread_or_shipment(world) -> None:
    db, clock, _ = world
    ctx = build_context(db, "SHP1006", eta_at("11:20"), clock.now())
    state = build_facility_state(db, ctx.facility_id, clock.now())
    with pytest.raises(AuthError) as err:
        record_option_set(db, ctx, "THR002", [], state, clock)  # THR002 belongs to DRV012
    assert err.value.code == "FOREIGN_THREAD"
    foreign = build_context(db, "SHP1012", eta_at("11:10"), clock.now())
    with pytest.raises(AuthError):
        record_option_set(db, foreign.model_copy(update={"driver_id": "DRV006"}), "THR001", [], state, clock)


# --- reconciliation ----------------------------------------------------------------------------
def test_unchanged_options_reconcile_clean(world) -> None:
    db, clock, _ = world
    s = show(db, clock)
    clock.advance(timedelta(minutes=1))
    diff = reconcile_option_set(db, s, clock)
    assert diff.all_unchanged and diff.still_offerable == (1, 2, 3)


def test_option_taken_by_someone_else_is_explained(world) -> None:
    db, clock, _ = world
    s = show(db, clock)
    taken = s.items[0].slot_id
    db.execute(
        "insert into appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source, "
        "is_current, booked_at, updated_at) values ('APT-X','SHP1018',?, 'CONFIRMED','PLANNER',1, "
        "'2026-08-04T09:40:00+05:30','2026-08-04T09:40:00+05:30')", (taken,))
    db.commit()
    diff = reconcile_option_set(db, s, clock)
    first = diff.items[0]
    assert first.change == "NEWLY_OCCUPIED" and "SLOT_OCCUPIED" in first.reason_codes
    assert "booked by someone else" in first.explanation
    assert [i.change for i in diff.items[1:]] == ["UNCHANGED", "UNCHANGED"]
    assert diff.still_offerable == (2, 3) and not diff.all_unchanged


def test_option_blocked_after_it_was_shown(world) -> None:
    db, clock, _ = world
    s = show(db, clock)
    db.execute("update appointment_slots set slot_status='BLOCKED', block_reason='forklift' where slot_id=?",
               (s.items[1].slot_id,))
    db.commit()
    diff = reconcile_option_set(db, s, clock)
    assert diff.items[1].change == "NEWLY_BLOCKED" and "SLOT_BLOCKED" in diff.items[1].reason_codes


def test_dock_breakdown_after_showing_blocks_the_option(world) -> None:
    db, clock, _ = world
    s = show(db, clock)
    dock = s.items[0].decision.dock_id
    db.execute(
        "insert into dock_status_events (dock_event_id, dock_id, event_type, event_start_ts, event_end_ts, "
        "reason, created_at) values ('DEVT-X',?, 'BREAKDOWN','2026-08-04T09:40:00+05:30',"
        "'2026-08-04T23:00:00+05:30','leveller','2026-08-04T09:40:00+05:30')", (dock,))
    db.commit()
    diff = reconcile_option_set(db, s, clock)
    assert diff.items[0].change == "NEWLY_BLOCKED" and "DOCK_EVENT_BLOCK" in diff.items[0].reason_codes


def test_slot_removed_from_schedule_disappears(world) -> None:
    db, clock, _ = world
    s = show(db, clock)
    gone = s.items[2].slot_id
    db.execute("pragma foreign_keys=off")
    db.execute("delete from appointment_slots where slot_id=?", (gone,))
    db.commit()
    db.execute("pragma foreign_keys=on")
    diff = reconcile_option_set(db, s, clock)
    assert diff.items[2].change == "DISAPPEARED" and "removed" in diff.items[2].explanation


def test_option_held_by_another_driver_is_newly_held(world) -> None:
    db, clock, _ = world
    s = show(db, clock)
    state = build_facility_state(db, s.facility_id, clock.now())
    held = with_state(state, occupancy={**state.occupancy, s.items[0].slot_id: Occupant("H1", "SHP9999", "HELD")})
    diff = diff_option_set(s, clock.now(), held)
    assert diff.items[0].change == "NEWLY_HELD" and "held for another driver" in diff.items[0].explanation


def test_dock_no_longer_suits_the_load_is_newly_incompatible(world) -> None:
    db, clock, _ = world
    s = show(db, clock)
    db.execute("update docks set max_vehicle_weight_kg=5000 where dock_id=?", (s.items[0].decision.dock_id,))
    db.commit()
    diff = reconcile_option_set(db, s, clock)
    assert diff.items[0].change == "NEWLY_INCOMPATIBLE"
    assert "WEIGHT_EXCEEDS_DOCK_LIMIT" in diff.items[0].reason_codes


def test_option_whose_start_has_passed_is_no_longer_feasible(world) -> None:
    db, clock, _ = world
    s = show(db, clock)
    clock.set(s.items[0].decision.slot_start + timedelta(minutes=5))
    diff = reconcile_option_set(db, s, clock)
    assert diff.items[0].change == "NO_LONGER_FEASIBLE" and "SLOT_IN_PAST" in diff.items[0].reason_codes
    assert diff.expired


def test_expiry_flag_follows_the_clock(world) -> None:
    db, clock, _ = world
    s = show(db, clock)
    clock.advance(OPTION_TTL - timedelta(seconds=1))
    assert not reconcile_option_set(db, s, clock).expired
    clock.advance(timedelta(seconds=1))
    assert reconcile_option_set(db, s, clock).expired


def test_changed_facility_rules_are_flagged(world) -> None:
    db, clock, _ = world
    s = show(db, clock)
    db.execute("update facility_rules set rule_value='20:00' where rule_type='LAST_NEW_START_TIME'")
    db.commit()
    diff = reconcile_option_set(db, s, clock)
    assert diff.rules_changed and not diff.all_unchanged


# --- retention -----------------------------------------------------------------------------------
def test_purge_removes_only_sets_older_than_retention(world) -> None:
    db, clock, _ = world
    old = show(db, clock)
    clock.advance(timedelta(days=31))
    recent = show(db, clock)
    removed = purge_option_sets(db, clock.now() - timedelta(days=30))
    assert removed == 1
    assert db.execute("select count(*) from option_sets where option_set_id=?", (old.option_set_id,)).fetchone()[0] == 0
    assert db.execute("select count(*) from option_set_items where option_set_id=?", (old.option_set_id,)).fetchone()[0] == 0
    assert get_latest_option_set(db, "DRV006", "THR001", "SHP1006").option_set_id == recent.option_set_id  # type: ignore[union-attr]


def test_purge_is_idempotent(world) -> None:
    db, clock, _ = world
    show(db, clock)
    clock.advance(timedelta(days=40))
    assert purge_option_sets(db, clock.now() - timedelta(days=30)) == 1
    assert purge_option_sets(db, clock.now() - timedelta(days=30)) == 0
