"""Phase 5: holds inside the appointment capacity boundary."""

from __future__ import annotations

import sqlite3
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from setuhaul import booking
from setuhaul.booking import OutcomeClass, acquire_hold, get_hold, release_hold
from setuhaul.capacity import build_facility_state
from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection
from setuhaul.feasibility import build_context, search_feasible_slots
from setuhaul.identity import AuthError
from tests.helpers import at, eta_at, seed_everything_known, show_options

NOW_PLUS = timedelta(seconds=180)


@pytest.fixture()
def world(db: sqlite3.Connection, clock: FakeClock):
    seed_everything_known(db)
    return db, clock


def hold(db, clock, option_set, rank=1, driver="DRV006", thread="THR001", shipment="SHP1006"):  # type: ignore[no-untyped-def]
    return acquire_hold(db, driver, thread, shipment, option_set.option_set_id, rank, clock)


# --- the shared capacity boundary ------------------------------------------------------------
def test_migration_adds_held_and_preserves_existing_rows(db: sqlite3.Connection) -> None:
    seed = open_connection(readonly=True)
    cols = "appointment_id, shipment_id, slot_id, appointment_status, is_current, booked_at, updated_at"
    before = seed.execute(f"select {cols} from appointments order by 1").fetchall()
    after = db.execute(f"select {cols} from appointments order by 1").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
    assert db.execute("pragma foreign_key_check").fetchall() == []
    assert db.execute("select count(*) from v_slot_availability").fetchone()[0] == 106
    assert db.execute("select count(*) from v_inbound_operational_state").fetchone()[0] == 21


def test_unique_indexes_cover_holds_in_the_same_boundary(db: sqlite3.Connection) -> None:
    sql = {r["name"]: r["sql"] for r in db.execute("select name, sql from sqlite_master where type='index'")}
    assert "'HELD'" in sql["ux_active_appointment_per_slot"]
    assert "'HELD'" not in sql["ux_current_active_appointment_per_shipment"]
    assert "appointment_status = 'HELD'" in sql["ux_one_hold_per_shipment"]


def test_hold_is_an_appointment_row_and_original_stays_active(world) -> None:
    db, clock = world
    opts = show_options(db, clock)
    r = hold(db, clock, opts)
    assert r.status == "HELD" and r.outcome is OutcomeClass.SUCCESS
    row = db.execute("select * from appointments where appointment_id=?", (r.appointment_id,)).fetchone()
    assert (row["appointment_status"], row["is_current"], row["slot_id"]) == ("HELD", 0, opts.items[0].slot_id)
    assert row["replaced_appointment_id"] == "APT1006"
    orig = db.execute("select appointment_status, is_current, slot_id from appointments where appointment_id='APT1006'").fetchone()
    assert tuple(orig) == ("CONFIRMED", 1, "SLOT-JAI-045")
    meta = db.execute("select * from appointment_hold_meta where appointment_id=?", (r.appointment_id,)).fetchone()
    assert (meta["driver_id"], meta["thread_id"], meta["option_set_id"]) == ("DRV006", "THR001", opts.option_set_id)
    assert meta["expires_epoch"] == int((clock.now() + NOW_PLUS).timestamp())
    assert r.expires_at == clock.now() + NOW_PLUS


def test_external_writer_hits_the_same_uniqueness_constraint(world) -> None:
    db, clock = world
    opts = show_options(db, clock)
    hold(db, clock, opts)
    slot = opts.items[0].slot_id
    for status in ("CONFIRMED", "PENDING_CONFIRMATION", "IN_PROGRESS", "HELD"):
        with pytest.raises(sqlite3.IntegrityError, match="appointments.slot_id"):
            db.execute(
                "insert into appointments (appointment_id, shipment_id, slot_id, appointment_status, "
                "booking_source, is_current, booked_at, updated_at) values (?,?,?,?, 'PLANNER', 0, "
                "'2026-08-04T09:36:00+05:30','2026-08-04T09:36:00+05:30')",
                (f"EXT-{status}", "SHP1018", slot, status),
            )
        db.rollback()


def test_hold_cannot_be_taken_on_a_slot_an_external_writer_booked_first(world) -> None:
    db, clock = world
    opts = show_options(db, clock)
    db.execute(
        "insert into appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source, "
        "is_current, booked_at, updated_at) values ('EXT-1','SHP1018',?, 'CONFIRMED','PLANNER',1, "
        "'2026-08-04T09:36:00+05:30','2026-08-04T09:36:00+05:30')", (opts.items[0].slot_id,))
    db.commit()
    r = hold(db, clock, opts)
    assert r.status == "SLOT_CONFLICT" and r.outcome is OutcomeClass.BUSINESS_CONFLICT


# --- simultaneous requests: first successful commit wins -------------------------------------
def test_second_valid_request_gets_slot_conflict(world) -> None:
    db, clock = world
    a = show_options(db, clock, "THR001", "SHP1006", "11:20")
    b = show_options(db, clock, "THR002", "SHP1012", "11:10")
    assert a.items[0].slot_id == b.items[0].slot_id  # both were shown the same best slot
    first = hold(db, clock, a)
    second = hold(db, clock, b, driver="DRV012", thread="THR002", shipment="SHP1012")
    assert first.status == "HELD"
    assert second.status == "SLOT_CONFLICT" and second.outcome is OutcomeClass.BUSINESS_CONFLICT
    assert second.diff is not None and second.diff.change == "NEWLY_HELD"
    assert db.execute("select count(*) from appointments where slot_id=? and appointment_status='HELD'",
                      (a.items[0].slot_id,)).fetchone()[0] == 1


def test_unique_index_is_the_backstop_when_the_read_was_stale(world, monkeypatch) -> None:
    db, clock = world
    a = show_options(db, clock, "THR001", "SHP1006", "11:20")
    b = show_options(db, clock, "THR002", "SHP1012", "11:10")
    stale = build_facility_state(db, b.facility_id, clock.now())  # captured before the first hold
    hold(db, clock, a)
    monkeypatch.setattr(booking, "build_facility_state", lambda *args, **kw: stale)
    second = hold(db, clock, b, driver="DRV012", thread="THR002", shipment="SHP1012")
    assert second.status == "SLOT_CONFLICT" and second.outcome is OutcomeClass.BUSINESS_CONFLICT
    assert db.execute("select count(*) from appointments where appointment_status='HELD'").fetchone()[0] == 1


def test_other_drivers_no_longer_see_a_held_slot(world) -> None:
    db, clock = world
    a = show_options(db, clock, "THR001", "SHP1006", "11:20")
    b_before = show_options(db, clock, "THR002", "SHP1012", "11:10")
    hold(db, clock, a)
    ctx = build_context(db, "SHP1012", eta_at("11:10"), clock.now())
    state = build_facility_state(db, ctx.facility_id, clock.now())
    out = search_feasible_slots(ctx, state)
    assert a.items[0].slot_id not in {d.slot_id for d in out.feasible}
    assert out.rejection_counts["SLOT_HELD"] == 1
    from setuhaul.option_history import reconcile_option_set

    diff = reconcile_option_set(db, b_before, clock)
    assert diff.items[0].change == "NEWLY_HELD"


# --- one hold per shipment -------------------------------------------------------------------
def test_one_shipment_cannot_hold_two_slots(world) -> None:
    db, clock = world
    opts = show_options(db, clock)
    first = hold(db, clock, opts, rank=1)
    second = hold(db, clock, opts, rank=2)
    assert second.status == "SHIPMENT_ALREADY_HELD" and second.existing_appointment_id == first.appointment_id
    assert db.execute("select count(*) from appointments where shipment_id='SHP1006' and appointment_status='HELD'").fetchone()[0] == 1


def test_database_itself_rejects_a_second_hold_for_the_shipment(world) -> None:
    db, clock = world
    opts = show_options(db, clock)
    hold(db, clock, opts, rank=1)
    with pytest.raises(sqlite3.IntegrityError, match="appointments.shipment_id"):
        db.execute(
            "insert into appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source, "
            "is_current, booked_at, updated_at) values ('H2','SHP1006',?, 'HELD','DRIVER_CHAT',0, "
            "'2026-08-04T09:36:00+05:30','2026-08-04T09:36:00+05:30')", (opts.items[1].slot_id,))
    db.rollback()


def test_repeating_the_same_hold_request_returns_the_same_hold(world) -> None:
    db, clock = world
    opts = show_options(db, clock)
    first, again = hold(db, clock, opts), hold(db, clock, opts)
    assert again.status == "HELD" and again.appointment_id == first.appointment_id
    assert db.execute("select count(*) from appointments where appointment_status='HELD'").fetchone()[0] == 1


def test_one_driver_can_hold_for_two_different_shipments(world) -> None:
    db, clock = world
    a = show_options(db, clock, "THR001", "SHP1006", "11:20")
    h1 = hold(db, clock, a)
    b = show_options(db, clock, "THR001", "SHP1021", "11:20")  # searched after the first hold
    assert h1.status == "HELD" and b.items
    h2 = acquire_hold(db, "DRV006", "THR001", "SHP1021", b.option_set_id, 1, clock)
    assert h2.status == "HELD" and h2.appointment_id != h1.appointment_id
    slots = {r[0] for r in db.execute("select slot_id from appointments where appointment_status='HELD'")}
    assert len(slots) == 2


# --- refusing stale or foreign input ---------------------------------------------------------
def test_expired_option_set_is_refused(world) -> None:
    db, clock = world
    opts = show_options(db, clock)
    clock.advance(timedelta(minutes=15))
    r = hold(db, clock, opts)
    assert r.status == "OPTION_EXPIRED" and r.outcome is OutcomeClass.BUSINESS_CONFLICT
    assert db.execute("select count(*) from appointments where appointment_status='HELD'").fetchone()[0] == 0


def test_option_that_changed_since_it_was_shown_is_not_held(world) -> None:
    db, clock = world
    opts = show_options(db, clock)
    db.execute("update appointment_slots set slot_status='BLOCKED', block_reason='x' where slot_id=?",
               (opts.items[0].slot_id,))
    db.commit()
    r = hold(db, clock, opts)
    assert r.status == "OPTION_CHANGED" and r.diff is not None and r.diff.change == "NEWLY_BLOCKED"


def test_unknown_rank_is_rejected(world) -> None:
    db, clock = world
    r = hold(db, clock, show_options(db, clock), rank=9)
    assert r.status == "INVALID_OPTION" and r.outcome is OutcomeClass.BUSINESS_CONFLICT


def test_option_set_of_another_driver_thread_or_shipment_cannot_be_used(world) -> None:
    db, clock = world
    opts = show_options(db, clock)
    for kw in ({"driver": "DRV007"}, {"thread": "THR002"}, {"shipment": "SHP1021"}):
        with pytest.raises(AuthError) as err:
            hold(db, clock, opts, **kw)
        assert err.value.code == "FOREIGN_OPTION_SET"


# --- release and inspection ------------------------------------------------------------------
def test_release_frees_capacity_and_leaves_original_untouched(world) -> None:
    db, clock = world
    a = show_options(db, clock, "THR001", "SHP1006", "11:20")
    r = hold(db, clock, a)
    out = release_hold(db, "DRV006", "THR001", r.appointment_id, "DRIVER_CANCELLED", clock)
    assert out.status == "RELEASED"
    row = db.execute("select appointment_status, cancellation_reason from appointments where appointment_id=?", (r.appointment_id,)).fetchone()
    assert tuple(row) == ("CANCELLED", "HOLD_RELEASED")
    meta = db.execute("select release_reason, released_at from appointment_hold_meta where appointment_id=?", (r.appointment_id,)).fetchone()
    assert meta["release_reason"] == "DRIVER_CANCELLED" and meta["released_at"]
    assert db.execute("select appointment_status from appointments where appointment_id='APT1006'").fetchone()[0] == "CONFIRMED"
    b = show_options(db, clock, "THR002", "SHP1012", "11:10")
    assert b.items[0].slot_id == a.items[0].slot_id  # the slot is open again
    assert acquire_hold(db, "DRV012", "THR002", "SHP1012", b.option_set_id, 1, clock).status == "HELD"
    assert release_hold(db, "DRV006", "THR001", r.appointment_id, "DRIVER_CANCELLED", clock).status == "ALREADY_RELEASED"


def test_release_requires_the_owner(world) -> None:
    db, clock = world
    r = hold(db, clock, show_options(db, clock))
    with pytest.raises(AuthError) as err:
        release_hold(db, "DRV007", "THR001", r.appointment_id, "DRIVER_CANCELLED", clock)
    assert err.value.code == "FOREIGN_HOLD"
    with pytest.raises(AuthError):
        release_hold(db, "DRV006", "THR002", r.appointment_id, "DRIVER_CANCELLED", clock)


def test_get_hold_reports_live_expired_and_released(world) -> None:
    db, clock = world
    r = hold(db, clock, show_options(db, clock))
    live = get_hold(db, r.appointment_id, clock)
    assert live is not None and live.state == "LIVE" and live.driver_id == "DRV006"
    clock.advance(NOW_PLUS - timedelta(seconds=1))
    live_state = get_hold(db, r.appointment_id, clock)
    assert live_state is not None and live_state.state == "LIVE"
    clock.advance(timedelta(seconds=1))
    expired = get_hold(db, r.appointment_id, clock)
    assert expired is not None and expired.state == "EXPIRED"  # even before any sweep
    assert get_hold(db, "NOPE", clock) is None


def test_every_hold_transition_is_in_immutable_history(world) -> None:
    db, clock = world
    r = hold(db, clock, show_options(db, clock))
    release_hold(db, "DRV006", "THR001", r.appointment_id, "DRIVER_CANCELLED", clock)
    types = [x["event_type"] for x in db.execute("select event_type from appointment_events where appointment_id=? order by event_id", (r.appointment_id,))]
    assert types == ["HOLD_ACQUIRED", "HOLD_RELEASED"]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("update appointment_events set reason='x'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("delete from appointment_events")
    db.rollback()


# --- lock timeouts are not business conflicts ------------------------------------------------
def test_lock_timeout_is_classified_separately(working_db_path: Path, clock: FakeClock) -> None:
    db = open_connection(path=working_db_path)
    seed_everything_known(db)
    opts = show_options(db, clock)
    db.execute("pragma busy_timeout=50")
    blocker = open_connection(path=working_db_path)
    blocker.execute("begin immediate")  # another writer holds the lock
    try:
        r = hold(db, clock, opts)
    finally:
        blocker.rollback()
        blocker.close()
    assert r.status == "LOCK_TIMEOUT" and r.outcome is OutcomeClass.LOCK_TIMEOUT
    assert r.outcome is not OutcomeClass.BUSINESS_CONFLICT
    assert db.execute("select count(*) from appointments where appointment_status='HELD'").fetchone()[0] == 0
    db.close()


def test_racing_threads_produce_exactly_one_hold(tmp_path: Path, clock: FakeClock) -> None:
    path = make_working_db(tmp_path / "race.sqlite3")
    setup = open_connection(path=path)
    seed_everything_known(setup)
    a = show_options(setup, clock, "THR001", "SHP1006", "11:20")
    b = show_options(setup, clock, "THR002", "SHP1012", "11:10")
    setup.close()
    results: list[str] = []
    barrier = threading.Barrier(2)

    def worker(driver: str, thread: str, shipment: str, opt) -> None:  # type: ignore[no-untyped-def]
        conn = open_connection(path=path)
        barrier.wait()
        results.append(acquire_hold(conn, driver, thread, shipment, opt.option_set_id, 1, clock).status)
        conn.close()

    ts_ = [threading.Thread(target=worker, args=("DRV006", "THR001", "SHP1006", a)),
           threading.Thread(target=worker, args=("DRV012", "THR002", "SHP1012", b))]
    [t.start() for t in ts_]
    [t.join() for t in ts_]
    assert sorted(results) == ["HELD", "SLOT_CONFLICT"]
    check = open_connection(path=path)
    assert check.execute("select count(*) from appointments where appointment_status='HELD'").fetchone()[0] == 1
    check.close()


def test_at_helper_is_consistent() -> None:
    assert at("09:35").utcoffset() == timedelta(hours=5, minutes=30)
