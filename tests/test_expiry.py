"""Phase 5: hold expiry is driven by an injectable clock and has an owner and a driver outcome."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import fakeredis
import pytest

from setuhaul.booking import HOLD_EXPIRED_MESSAGE, acquire_hold, get_hold, make_hold_checker
from setuhaul.clock import FakeClock
from setuhaul.config import load_settings
from setuhaul.db import open_connection
from setuhaul.jobs.expire_holds import MaintenanceRunner, expire_due_holds
from setuhaul.memory import ConversationMemoryStore, HoldRef
from setuhaul.option_history import get_latest_option_set
from tests.helpers import seed_everything_known, show_options

TTL = timedelta(seconds=180)


@pytest.fixture()
def world(db: sqlite3.Connection, clock: FakeClock):
    seed_everything_known(db)
    return db, clock


def make_hold(db, clock, thread="THR001", shipment="SHP1006", driver="DRV006", eta="11:20"):  # type: ignore[no-untyped-def]
    opts = show_options(db, clock, thread, shipment, eta)
    r = acquire_hold(db, driver, thread, shipment, opts.option_set_id, 1, clock)
    assert r.status == "HELD"
    return opts, r


def test_return_before_expiry_keeps_the_hold(world) -> None:
    db, clock = world
    _, r = make_hold(db, clock)
    clock.advance(TTL - timedelta(seconds=1))
    out = expire_due_holds(db, clock)
    assert out.expired == 0 and get_hold(db, r.appointment_id, clock).state == "LIVE"  # type: ignore[union-attr]


def test_exactly_at_expiry_the_hold_lapses(world) -> None:
    db, clock = world
    _, r = make_hold(db, clock)
    clock.advance(TTL)
    out = expire_due_holds(db, clock)
    assert out.expired == 1 and out.appointment_ids == (r.appointment_id,)
    assert db.execute("select appointment_status from appointments where appointment_id=?", (r.appointment_id,)).fetchone()[0] == "CANCELLED"


def test_after_expiry_capacity_is_released_and_original_untouched(world) -> None:
    db, clock = world
    opts, r = make_hold(db, clock)
    clock.advance(TTL + timedelta(minutes=5))
    expire_due_holds(db, clock)
    orig = db.execute("select appointment_status, is_current, slot_id from appointments where appointment_id='APT1006'").fetchone()
    assert tuple(orig) == ("CONFIRMED", 1, "SLOT-JAI-045")  # "no booking changed"
    meta = db.execute("select release_reason, released_at from appointment_hold_meta where appointment_id=?", (r.appointment_id,)).fetchone()
    assert meta["release_reason"] == "EXPIRED" and meta["released_at"]
    other = show_options(db, clock, "THR002", "SHP1012", "11:10")
    assert other.items[0].slot_id == opts.items[0].slot_id
    assert acquire_hold(db, "DRV012", "THR002", "SHP1012", other.option_set_id, 1, clock).status == "HELD"


def test_expiry_appends_history_and_queues_exactly_one_driver_notice(world) -> None:
    db, clock = world
    _, r = make_hold(db, clock)
    clock.advance(TTL)
    expire_due_holds(db, clock)
    ev = db.execute("select * from appointment_events where appointment_id=? and event_type='HOLD_EXPIRED'", (r.appointment_id,)).fetchall()
    assert len(ev) == 1 and (ev[0]["from_status"], ev[0]["to_status"], ev[0]["actor_type"]) == ("HELD", "CANCELLED", "SYSTEM")
    box = db.execute("select * from notification_outbox where dedupe_key=?", (f"HOLD_EXPIRED:{r.appointment_id}",)).fetchall()
    assert len(box) == 1 and box[0]["recipient_type"] == "DRIVER" and box[0]["recipient_ref"] == "DRV006"
    payload = json.loads(box[0]["payload_json"])
    assert payload["text"] == HOLD_EXPIRED_MESSAGE and payload["thread_id"] == "THR001"


def test_driver_text_states_lapse_no_change_and_fresh_search() -> None:
    low = HOLD_EXPIRED_MESSAGE.lower()
    assert "lapsed" in low and "no booking has changed" in low and "fresh options" in low


def test_duplicate_sweep_is_idempotent(world) -> None:
    db, clock = world
    make_hold(db, clock)
    clock.advance(TTL)
    first, second = expire_due_holds(db, clock), expire_due_holds(db, clock)
    assert (first.expired, second.expired) == (1, 0)
    assert db.execute("select count(*) from appointment_events where event_type='HOLD_EXPIRED'").fetchone()[0] == 1
    assert db.execute("select count(*) from notification_outbox").fetchone()[0] == 1


def test_two_runners_on_separate_connections_do_not_double_expire(world, working_db_path: Path) -> None:
    db, clock = world
    make_hold(db, clock)
    clock.advance(TTL)
    other = open_connection(path=working_db_path)
    a, b = expire_due_holds(db, clock), expire_due_holds(other, clock)
    other.close()
    assert a.expired + b.expired == 1


def test_restart_with_several_overdue_holds_and_a_batch_limit(world) -> None:
    db, clock = world
    make_hold(db, clock, "THR001", "SHP1006", "DRV006", "11:20")
    make_hold(db, clock, "THR002", "SHP1012", "DRV012", "11:10")
    make_hold(db, clock, "THR003", "SHP1013", "DRV013", "11:00")
    clock.advance(timedelta(hours=2))  # process was down; all three are overdue
    assert expire_due_holds(db, clock, limit=2).expired == 2
    assert expire_due_holds(db, clock, limit=2).expired == 1
    assert expire_due_holds(db, clock, limit=2).expired == 0


def test_lazy_expiry_lets_the_next_driver_in_before_any_sweep(world) -> None:
    db, clock = world
    opts, _ = make_hold(db, clock)
    before = show_options(db, clock, "THR002", "SHP1012", "11:10")  # slot is held, so it is not offered
    assert before.items[0].slot_id != opts.items[0].slot_id
    clock.advance(TTL)
    fresh = show_options(db, clock, "THR002", "SHP1012", "11:10")
    assert fresh.items[0].slot_id == opts.items[0].slot_id  # expired hold no longer consumes capacity
    r = acquire_hold(db, "DRV012", "THR002", "SHP1012", fresh.option_set_id, 1, clock)
    assert r.status == "HELD"
    assert db.execute("select count(*) from appointment_events where event_type='HOLD_EXPIRED'").fetchone()[0] == 1


def test_memory_reload_sees_live_hold_state_not_a_stale_promise(world) -> None:
    db, clock = world
    _, r = make_hold(db, clock)
    store = ConversationMemoryStore(db, fakeredis.FakeRedis(decode_responses=True), clock, load_settings(),
                                    hold_checker=make_hold_checker(db, clock))
    ctx = store.load("DRV006", "THR001")
    store.save_structured_state("DRV006", "THR001", ctx.state.model_copy(update={"hold": HoldRef(
        appointment_id=r.appointment_id, expires_at=r.expires_at)}))  # type: ignore[arg-type]
    clock.advance(timedelta(seconds=60))
    assert store.load("DRV006", "THR001").events == []
    clock.advance(TTL)
    back = store.load("DRV006", "THR001")
    assert [e.type for e in back.events] == ["HOLD_EXPIRED"] and back.state.hold is None
    expire_due_holds(db, clock)  # sweep afterwards: the event stays correct
    assert [e.type for e in store.load("DRV006", "THR001").events] == ["HOLD_EXPIRED"]


def test_maintenance_runner_expires_and_purges_option_sets_once_a_day(world) -> None:
    db, clock = world
    show_options(db, clock)  # will be 31 days old
    make_hold(db, clock, "THR002", "SHP1012", "DRV012", "11:10")
    clock.advance(timedelta(days=31))
    runner = MaintenanceRunner(db, clock)
    first = runner.run_once()
    assert first.expired == 1 and first.purged_option_sets >= 1
    assert get_latest_option_set(db, "DRV006", "THR001", "SHP1006") is None
    show_options(db, clock, "THR001", "SHP1006", "11:20", limit=1)
    clock.set(clock.now() + timedelta(days=0, hours=23, minutes=59))
    again = runner.run_once()
    assert again.purged_option_sets == 0  # not yet 24 hours since the last purge
    clock.advance(timedelta(days=31, hours=2))
    assert runner.run_once().purged_option_sets == 1


def test_runner_survives_an_empty_database_state(world) -> None:
    db, clock = world
    out = MaintenanceRunner(db, clock).run_once()
    assert out.expired == 0 and out.purged_option_sets == 0


def test_background_loop_sweeps_on_its_own_connection(world, working_db_path: Path) -> None:
    import time

    db, clock = world
    make_hold(db, clock)
    clock.advance(TTL)
    runner = MaintenanceRunner(db, clock, interval_seconds=0.02, conn_factory=lambda: open_connection(path=working_db_path))
    runner.start()  # startup sweep happens synchronously
    try:
        assert db.execute("select count(*) from appointments where appointment_status='HELD'").fetchone()[0] == 0
        make_hold(db, clock, "THR002", "SHP1012", "DRV012", "11:10")
        clock.advance(TTL)
        deadline = time.time() + 5
        while time.time() < deadline and db.execute("select count(*) from appointments where appointment_status='HELD'").fetchone()[0]:
            time.sleep(0.05)
    finally:
        runner.stop()
    assert db.execute("select count(*) from appointments where appointment_status='HELD'").fetchone()[0] == 0


def test_start_without_a_connection_factory_is_refused(world) -> None:
    db, clock = world
    with pytest.raises(RuntimeError):
        MaintenanceRunner(db, clock).start()
