"""Contention harness: run requests simultaneously from threads or separate processes.

Every request gets its own connection (WAL, documented busy timeout), waits on a barrier so they
start together, and is classified SUCCESS / BUSINESS_CONFLICT / LOCK_TIMEOUT / SYSTEM_ERROR. A lock
timeout is never reported as a conflict or a success.
"""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from setuhaul.booking import (
    ConfirmCommand,
    OutcomeClass,
    acquire_hold,
    confirm_reschedule,
    release_hold,
)
from setuhaul.cancellation import cancel_confirmed_appointment, cancel_pending_change
from setuhaul.clock import FakeClock
from setuhaul.db import open_connection, parse_operational_timestamp
from setuhaul.identity import AuthError
from setuhaul.integrity import InvariantViolation, check_invariants
from setuhaul.jobs.expire_holds import expire_due_holds


@dataclass(frozen=True)
class Op:
    kind: str  # hold | release | confirm | cancel | cancel_pending | sweep
    params: dict[str, Any]
    now_iso: str
    role: str = ""


@dataclass(frozen=True)
class OpOutcome:
    role: str
    kind: str
    status: str
    outcome: OutcomeClass
    pid: int
    journal_mode: str
    busy_timeout_ms: int


@dataclass
class CompetitionReport:
    outcomes: list[OpOutcome]
    violations: list[InvariantViolation] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return dict(Counter(o.outcome.value for o in self.outcomes))


def execute_op(conn: sqlite3.Connection, op: Op) -> tuple[str, OutcomeClass]:
    clock = FakeClock(parse_operational_timestamp(op.now_iso))
    p = op.params
    try:
        if op.kind == "hold":
            r = acquire_hold(conn, p["driver_id"], p["thread_id"], p["shipment_id"], p["option_set_id"], p["rank"], clock)
            return r.status, r.outcome
        if op.kind == "release":
            rel = release_hold(conn, p["driver_id"], p["thread_id"], p["appointment_id"], p["reason"], clock)
            return rel.status, rel.outcome
        if op.kind == "confirm":
            c = confirm_reschedule(conn, ConfirmCommand(**p), clock)
            return c.status, c.outcome
        if op.kind == "cancel":
            k = cancel_confirmed_appointment(conn, p["driver_id"], p["thread_id"], p["message_id"], p["shipment_id"], clock)
            return k.status, k.outcome
        if op.kind == "cancel_pending":
            pc = cancel_pending_change(conn, p["driver_id"], p["thread_id"], p["shipment_id"], clock)
            return pc.status, OutcomeClass.SUCCESS
        if op.kind == "sweep":
            return f"SWEPT:{expire_due_holds(conn, clock).expired}", OutcomeClass.SUCCESS
        raise ValueError(f"unknown op kind {op.kind!r}")
    except AuthError as exc:
        return f"AUTH:{exc.code}", OutcomeClass.BUSINESS_CONFLICT
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg:
            return "LOCK_TIMEOUT", OutcomeClass.LOCK_TIMEOUT
        return f"ERROR:{exc}", OutcomeClass.SYSTEM_ERROR
    except Exception as exc:  # noqa: BLE001 - the harness classifies every failure instead of crashing the run
        return f"ERROR:{type(exc).__name__}", OutcomeClass.SYSTEM_ERROR


def _run_one(path: str, op: Op, barrier: Any, busy_ms: int) -> OpOutcome:
    conn = open_connection(path=Path(path))
    conn.execute(f"pragma busy_timeout={busy_ms}")
    journal = str(conn.execute("pragma journal_mode").fetchone()[0])
    busy = int(conn.execute("pragma busy_timeout").fetchone()[0])
    try:
        barrier.wait(timeout=120)
        status, outcome = execute_op(conn, op)
    finally:
        conn.close()
    return OpOutcome(op.role, op.kind, status, outcome, os.getpid(), journal, busy)


def _process_main(path: str, op: Op, barrier: Any, queue: Any, busy_ms: int) -> None:
    queue.put(_run_one(path, op, barrier, busy_ms))


def run_competing_requests(
    path: Path, ops: list[Op], mode: Literal["threads", "processes"] = "threads", busy_timeout_ms: int = 5000
) -> CompetitionReport:
    n = len(ops)
    outcomes: list[OpOutcome] = []
    if mode == "threads":
        barrier = threading.Barrier(n)
        slots: list[OpOutcome | None] = [None] * n

        def work(i: int) -> None:
            slots[i] = _run_one(str(path), ops[i], barrier, busy_timeout_ms)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        outcomes = [s for s in slots if s is not None]
    else:
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(n)
        queue = ctx.Queue()
        procs = [ctx.Process(target=_process_main, args=(str(path), op, barrier, queue, busy_timeout_ms)) for op in ops]
        for p in procs:
            p.start()
        outcomes = [queue.get(timeout=180) for _ in procs]
        for p in procs:
            p.join(timeout=60)
    with open_connection(path=path) as check:
        violations = check_invariants(check)
    return CompetitionReport(outcomes, violations)
