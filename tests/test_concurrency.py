"""Phase 7: integrity under threaded, multiprocess and mixed contention on one SQLite file."""

from __future__ import annotations

import sqlite3
import time
from collections import Counter
from pathlib import Path

import pytest

from setuhaul.booking import OutcomeClass
from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection
from setuhaul.integrity import InvariantViolation, check_invariants
from setuhaul.proof import Op, run_competing_requests
from tests.helpers import at
from tests.scenarios import contention_world, mixed_world

CLASSES = {c.value for c in OutcomeClass}


@pytest.fixture()
def path(tmp_path: Path) -> Path:
    return make_working_db(tmp_path / "proof.sqlite3")


def names(violations: list[InvariantViolation]) -> set[str]:
    return {v.name for v in violations}


# --- the invariant checker itself: clean data passes, every planted fault is caught ---------------
def test_clean_snapshot_has_no_violations(path: Path) -> None:
    with open_connection(path=path) as conn:
        assert check_invariants(conn) == []


def plant(conn: sqlite3.Connection, sql: str) -> None:
    conn.execute("pragma foreign_keys=off")
    conn.executescript(sql)
    conn.execute("pragma foreign_keys=on")


INS = ("insert into appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source, "
       "is_current, booked_at, updated_at) values ")
STAMP = "'2026-08-04T09:00:00+05:30'"


@pytest.mark.parametrize(
    ("expected", "sql"),
    [
        ("DUPLICATE_ACTIVE_SLOT",
         f"drop index ux_active_appointment_per_slot; {INS}('X1','SHP1018','SLOT-JAI-002','CONFIRMED','PLANNER',0,{STAMP},{STAMP});"),
        ("DUPLICATE_CURRENT_APPOINTMENT",
         f"drop index ux_current_active_appointment_per_shipment; {INS}('X1','SHP1006','SLOT-JAI-019','CONFIRMED','PLANNER',1,{STAMP},{STAMP});"),
        ("MULTIPLE_HOLDS_PER_SHIPMENT",
         (f"drop index ux_one_hold_per_shipment; {INS}('H1','SHP1018','SLOT-JAI-019','HELD','DRIVER_CHAT',0,{STAMP},{STAMP}),"
         f"('H2','SHP1018','SLOT-JAI-020','HELD','DRIVER_CHAT',0,{STAMP},{STAMP});")),
        ("ORPHAN_HOLD_METADATA",
         ("insert into appointment_hold_meta (appointment_id, driver_id, thread_id, option_set_id, expires_at, "
         "expires_epoch, request_fingerprint) values ('GHOST','DRV006','THR001','O','2026-08-04T10:00:00+05:30',1,'f');")),
        ("HELD_WITHOUT_METADATA",
         f"{INS}('H1','SHP1018','SLOT-JAI-019','HELD','DRIVER_CHAT',0,{STAMP},{STAMP});"),
        ("HOLD_METADATA_STATE_MISMATCH",
         (f"{INS}('H1','SHP1018','SLOT-JAI-019','HELD','DRIVER_CHAT',0,{STAMP},{STAMP});"
         "insert into appointment_hold_meta (appointment_id, driver_id, thread_id, option_set_id, expires_at, "
         "expires_epoch, request_fingerprint, released_at, release_reason) values ('H1','DRV003','THR007','O',"
         "'2026-08-04T10:00:00+05:30',1,'f','2026-08-04T09:00:00+05:30','EXPIRED');")),
        ("HISTORY_WITHOUT_PARENT",
         ("insert into appointment_events (appointment_id, shipment_id, event_type, actor_type, occurred_at) "
         "values ('NOPE','SHP1006','HOLD_ACQUIRED','SYSTEM','2026-08-04T09:00:00+05:30');")),
        ("REPLACED_APPOINTMENT_STILL_ACTIVE",
         (f"{INS}('N1','SHP1018','SLOT-JAI-019','CONFIRMED','DRIVER_CHAT',1,{STAMP},{STAMP});"
         "update appointments set replaced_appointment_id='APT1007' where appointment_id='N1';")),
    ],
)
def test_each_planted_fault_is_detected(path: Path, expected: str, sql: str) -> None:
    with open_connection(path=path) as conn:
        plant(conn, sql)
        assert expected in names(check_invariants(conn))


def test_violation_carries_enough_detail_to_act_on(path: Path) -> None:
    with open_connection(path=path) as conn:
        plant(conn, f"drop index ux_active_appointment_per_slot; {INS}('X1','SHP1018','SLOT-JAI-002','CONFIRMED','PLANNER',0,{STAMP},{STAMP});")
        v = next(v for v in check_invariants(conn) if v.name == "DUPLICATE_ACTIVE_SLOT")
        assert "SLOT-JAI-002" in v.detail


# --- connections: WAL and a documented busy timeout ---------------------------------------------------
def test_every_worker_connection_uses_wal_and_the_busy_timeout(path: Path) -> None:
    w = contention_world(path, FakeClock(at("09:35")), n=4)
    report = run_competing_requests(path, w.ops, mode="threads")
    assert {o.journal_mode for o in report.outcomes} == {"wal"}
    assert {o.busy_timeout_ms for o in report.outcomes} == {5000}


# --- ten threads, one slot ------------------------------------------------------------------------------
def test_ten_synchronized_threads_one_slot_exactly_one_winner(path: Path) -> None:
    w = contention_world(path, FakeClock(at("09:35")), n=10)
    report = run_competing_requests(path, w.ops, mode="threads")
    counts = report.counts()
    assert counts == {"SUCCESS": 1, "BUSINESS_CONFLICT": 9}, counts
    assert [o.status for o in report.outcomes].count("HELD") == 1
    assert report.violations == []
    with open_connection(path=path) as conn:
        assert conn.execute("select count(*) from appointments where slot_id=? and appointment_status='HELD'",
                            (w.target_slot,)).fetchone()[0] == 1


def test_six_worker_processes_one_slot_exactly_one_winner(path: Path) -> None:
    w = contention_world(path, FakeClock(at("09:35")), n=6)
    report = run_competing_requests(path, w.ops, mode="processes")
    assert report.counts() == {"SUCCESS": 1, "BUSINESS_CONFLICT": 5}, report.counts()
    assert len({o.pid for o in report.outcomes}) == 6  # genuinely separate processes
    assert report.violations == []


# --- mixed case -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["threads", "processes"])
def test_mixed_contention_preserves_every_invariant(path: Path, mode: str) -> None:
    w = mixed_world(path, FakeClock(at("09:35")))
    report = run_competing_requests(path, w.ops, mode=mode)  # type: ignore[arg-type]
    assert report.violations == [], report.violations
    assert set(report.counts()) <= CLASSES
    assert report.counts().get("SYSTEM_ERROR", 0) == 0
    by_role = {}
    for o in report.outcomes:
        by_role.setdefault(o.role, []).append(o)
    contenders = by_role["contender"]
    assert sum(o.status == "HELD" for o in contenders) == 1  # same-slot contention: one winner
    assert by_role["release"][0].status == "RELEASED"
    assert by_role["expire"][0].status.startswith("SWEPT")
    assert by_role["confirm"][0].status == "CONFIRMED"
    assert by_role["cancel"][0].status == "CANCELLED"
    # the swap worker either lost the race (conflict) or took the slot the confirm freed; both are legal
    assert by_role["swap"][0].status in {"HELD", "SLOT_CONFLICT", "OPTION_CHANGED"}


def test_mixed_end_state_matches_the_committed_outcomes(path: Path) -> None:
    w = mixed_world(path, FakeClock(at("09:35")))
    report = run_competing_requests(path, w.ops, mode="threads")
    roles = {o.role: o for o in report.outcomes if o.role != "contender"}
    with open_connection(path=path) as conn:
        q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
        assert q("select appointment_status from appointments where appointment_id=?", w.refs["release_hold"]) == "CANCELLED"
        assert q("select appointment_status from appointments where appointment_id=?", w.refs["expire_hold"]) == "CANCELLED"
        assert q("select release_reason from appointment_hold_meta where appointment_id=?", w.refs["expire_hold"]) == "EXPIRED"
        assert q("select appointment_status from appointments where appointment_id=?", w.refs["confirm_hold"]) == "CONFIRMED"
        assert q("select appointment_status from appointments where appointment_id=?", w.refs["confirm_original"]) == "CANCELLED"
        assert q("select appointment_status from appointments where appointment_id=?", w.refs["cancel_appointment"]) == "CANCELLED"
        freed = w.refs["confirm_original_slot"]
        holder = conn.execute("select appointment_status from appointments where slot_id=? and appointment_status in "
                              "('HELD','CONFIRMED','PENDING_CONFIRMATION','IN_PROGRESS')", (freed,)).fetchall()
        assert len(holder) == (1 if roles["swap"].status == "HELD" else 0)


# --- lock timeouts are their own class -------------------------------------------------------------------
def test_lock_timeouts_are_never_reported_as_conflicts_or_success(path: Path) -> None:
    w = contention_world(path, FakeClock(at("09:35")), n=4)
    blocker = open_connection(path=path)
    blocker.execute("begin immediate")  # another writer sits on the lock for the whole run
    try:
        report = run_competing_requests(path, w.ops, mode="threads", busy_timeout_ms=50)
    finally:
        blocker.rollback()
        blocker.close()
    assert report.counts() == {"LOCK_TIMEOUT": 4}, report.counts()
    assert report.violations == []
    with open_connection(path=path) as conn:
        assert conn.execute("select count(*) from appointments where appointment_status='HELD'").fetchone()[0] == 0


def test_busy_timeout_expires_after_the_documented_wait(path: Path) -> None:
    blocker = open_connection(path=path)
    blocker.execute("begin immediate")
    conn = open_connection(path=path)
    conn.execute("pragma busy_timeout=300")
    start = time.perf_counter()
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        conn.execute("begin immediate")
    waited = time.perf_counter() - start
    blocker.rollback()
    blocker.close()
    conn.close()
    assert 0.2 < waited < 2.0


def test_counts_helper_merges_classes() -> None:
    from setuhaul.proof import CompetitionReport, OpOutcome

    mk = lambda c: OpOutcome("r", "hold", "S", c, 1, "wal", 5000)
    rep = CompetitionReport([mk(OutcomeClass.SUCCESS), mk(OutcomeClass.LOCK_TIMEOUT), mk(OutcomeClass.LOCK_TIMEOUT)], [])
    assert rep.counts() == {"SUCCESS": 1, "LOCK_TIMEOUT": 2}
    assert Counter(o.outcome for o in rep.outcomes)[OutcomeClass.LOCK_TIMEOUT] == 2
    assert Op("hold", {}, "2026-08-04T09:35:00+05:30").kind == "hold"
