"""Contention harness: run requests simultaneously from threads or separate processes.

Every request gets its own connection (WAL, documented busy timeout), waits on a barrier so they
start together, and is classified SUCCESS / BUSINESS_CONFLICT / LOCK_TIMEOUT / SYSTEM_ERROR. A lock
timeout is never reported as a conflict or a success.
"""

# Lazy annotations.
from __future__ import annotations

# multiprocessing starts separate OS processes, to prove locking works across processes and not only threads.
import multiprocessing

# os.getpid reports which process ran each operation.
import os

# sqlite3 for connections and error classes.
import sqlite3

# threading runs the thread-based contention mode and its barrier.
import threading

# Counter tallies outcomes.
from collections import Counter

# dataclasses for the report types.
from dataclasses import dataclass, field

# Paths.
from pathlib import Path

# Typing helpers.
from typing import Any, Literal

# The booking operations under test.
from setuhaul.booking import (
    ConfirmCommand,
    OutcomeClass,
    acquire_hold,
    confirm_reschedule,
    release_hold,
)

# Cancellation operations under test.
from setuhaul.cancellation import cancel_confirmed_appointment, cancel_pending_change

# A fixed clock per operation.
from setuhaul.clock import FakeClock

# Connection and timestamp helpers.
from setuhaul.db import open_connection, parse_operational_timestamp

# Authorisation error type.
from setuhaul.identity import AuthError

# Invariants checked after every run.
from setuhaul.integrity import InvariantViolation, check_invariants

# The expiry sweep.
from setuhaul.jobs.expire_holds import expire_due_holds


@dataclass(frozen=True)
# One operation to run: its kind, parameters and the moment it believes it is.
class Op:
    kind: str  # hold | release | confirm | cancel | cancel_pending | sweep
    params: dict[str, Any]
    now_iso: str
    role: str = ""


@dataclass(frozen=True)
# What one operation did, including the process id and database settings it ran with (evidence the contention was real).
class OpOutcome:
    role: str
    kind: str
    status: str
    outcome: OutcomeClass
    pid: int
    journal_mode: str
    busy_timeout_ms: int


@dataclass
# All outcomes plus any invariant violations found afterwards.
class CompetitionReport:
    outcomes: list[OpOutcome]
    violations: list[InvariantViolation] = field(default_factory=list)

    # How many of each outcome class (SUCCESS, BUSINESS_CONFLICT, ...).
    def counts(self) -> dict[str, int]:
        return dict(Counter(o.outcome.value for o in self.outcomes))


# Run one operation and classify its result. A busy database is never reported as a conflict or a success.
def execute_op(conn: sqlite3.Connection, op: Op) -> tuple[str, OutcomeClass]:
    # A fixed clock for this operation.
    clock = FakeClock(parse_operational_timestamp(op.now_iso))
    # Short name for the parameters.
    p = op.params
    try:
        # Place a hold.
        if op.kind == "hold":
            r = acquire_hold(conn, p["driver_id"], p["thread_id"], p["shipment_id"], p["option_set_id"], p["rank"], clock)
            return r.status, r.outcome
        # Release a hold.
        if op.kind == "release":
            rel = release_hold(conn, p["driver_id"], p["thread_id"], p["appointment_id"], p["reason"], clock)
            return rel.status, rel.outcome
        # Confirm a hold.
        if op.kind == "confirm":
            c = confirm_reschedule(conn, ConfirmCommand(**p), clock)
            return c.status, c.outcome
        # Cancel a confirmed appointment.
        if op.kind == "cancel":
            k = cancel_confirmed_appointment(conn, p["driver_id"], p["thread_id"], p["message_id"], p["shipment_id"], clock)
            return k.status, k.outcome
        # 'Never mind': release the pending hold.
        if op.kind == "cancel_pending":
            pc = cancel_pending_change(conn, p["driver_id"], p["thread_id"], p["shipment_id"], clock)
            return pc.status, OutcomeClass.SUCCESS
        # Run the expiry sweep.
        if op.kind == "sweep":
            return f"SWEPT:{expire_due_holds(conn, clock).expired}", OutcomeClass.SUCCESS
        # A harness bug, not a service outcome.
        raise ValueError(f"unknown op kind {op.kind!r}")
    # Ownership failures count as business conflicts.
    except AuthError as exc:
        return f"AUTH:{exc.code}", OutcomeClass.BUSINESS_CONFLICT
    # A busy or locked database is LOCK_TIMEOUT; other operational errors are SYSTEM_ERROR.
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg:
            return "LOCK_TIMEOUT", OutcomeClass.LOCK_TIMEOUT
        return f"ERROR:{exc}", OutcomeClass.SYSTEM_ERROR
    # Anything else is classified, not allowed to crash the run.
    except Exception as exc:  # noqa: BLE001 - the harness classifies every failure instead of crashing the run
        return f"ERROR:{type(exc).__name__}", OutcomeClass.SYSTEM_ERROR


# Run one operation on its own connection, after every competitor is ready.
def _run_one(path: str, op: Op, barrier: Any, busy_ms: int) -> OpOutcome:
    # A private connection (SQLite connections are not shared).
    conn = open_connection(path=Path(path))
    # The wait-for-lock limit for this run.
    conn.execute(f"pragma busy_timeout={busy_ms}")
    # Record the journal mode, as evidence WAL was in use.
    journal = str(conn.execute("pragma journal_mode").fetchone()[0])
    # Record the busy timeout actually in force.
    busy = int(conn.execute("pragma busy_timeout").fetchone()[0])
    try:
        # Wait until all competitors are ready, then start together.
        barrier.wait(timeout=120)
        # Run the operation.
        status, outcome = execute_op(conn, op)
    finally:
        # Always release the connection.
        conn.close()
    # Package the result.
    return OpOutcome(op.role, op.kind, status, outcome, os.getpid(), journal, busy)


# Entry point of a child process: run one operation and post the result to a queue.
def _process_main(path: str, op: Op, barrier: Any, queue: Any, busy_ms: int) -> None:
    queue.put(_run_one(path, op, barrier, busy_ms))


# Run all operations at the same moment, in threads or in separate processes, then check the whole database.
def run_competing_requests(
    path: Path, ops: list[Op], mode: Literal["threads", "processes"] = "threads", busy_timeout_ms: int = 5000
) -> CompetitionReport:
    # Number of competitors.
    n = len(ops)
    # Results.
    outcomes: list[OpOutcome] = []
    # Thread mode.
    if mode == "threads":
        # All threads wait here until every one has arrived.
        barrier = threading.Barrier(n)
        # One result slot per thread.
        slots: list[OpOutcome | None] = [None] * n

        # Each thread's job.
        def work(i: int) -> None:
            slots[i] = _run_one(str(path), ops[i], barrier, busy_timeout_ms)

        # Create the threads.
        threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Collect results in order.
        outcomes = [s for s in slots if s is not None]
    # Process mode.
    else:
        # 'spawn' starts clean processes (the same way on Windows and Linux).
        ctx = multiprocessing.get_context("spawn")
        # A barrier shared across processes.
        barrier = ctx.Barrier(n)
        # Children return results through this queue.
        queue = ctx.Queue()
        # Create one process per operation.
        procs = [ctx.Process(target=_process_main, args=(str(path), op, barrier, queue, busy_timeout_ms)) for op in ops]
        for p in procs:
            p.start()
        # Collect one result per process.
        outcomes = [queue.get(timeout=180) for _ in procs]
        for p in procs:
            p.join(timeout=60)
    # After the race, check every database invariant.
    with open_connection(path=path) as check:
        violations = check_invariants(check)
    # The outcomes plus any violations.
    return CompetitionReport(outcomes, violations)
