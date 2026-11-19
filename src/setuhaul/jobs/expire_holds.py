"""Hold expiry sweep and the prototype maintenance runner.

Owner: this job (plus lazy expiry inside ``acquire_hold``). In production it runs from an external
scheduler; the single-process prototype wires ``MaintenanceRunner.start`` into the FastAPI lifespan
in Phase 11. Every operation is idempotent, so several runners are safe.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from setuhaul.booking import expire_hold_in_txn
from setuhaul.clock import Clock
from setuhaul.config import load_settings
from setuhaul.db import immediate
from setuhaul.option_history import RETENTION, purge_option_sets

PURGE_JOB = "purge_option_sets"
PURGE_EVERY = timedelta(hours=24)


@dataclass(frozen=True)
class ExpiryBatchResult:
    expired: int
    appointment_ids: tuple[str, ...]


@dataclass(frozen=True)
class MaintenanceResult:
    expired: int
    purged_option_sets: int


def expire_due_holds(conn: sqlite3.Connection, clock: Clock, limit: int = 100) -> ExpiryBatchResult:
    """Expire up to ``limit`` holds whose TTL has elapsed (``expires_at <= now``, inclusive)."""
    now = clock.now()
    due = [
        r["appointment_id"]
        for r in conn.execute(
            "select a.appointment_id from appointments a join appointment_hold_meta m using(appointment_id) "
            "where a.appointment_status='HELD' and m.expires_epoch <= ? order by m.expires_epoch, a.appointment_id limit ?",
            (int(now.timestamp()), limit),
        )
    ]
    done: list[str] = []
    for appointment_id in due:
        with immediate(conn):  # one transaction per hold; re-checks status, so duplicates are no-ops
            if expire_hold_in_txn(conn, appointment_id, now):
                done.append(appointment_id)
    return ExpiryBatchResult(expired=len(done), appointment_ids=tuple(done))


class MaintenanceRunner:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock,
        interval_seconds: float | None = None,
        conn_factory: Callable[[], sqlite3.Connection] | None = None,
    ) -> None:
        self._conn = conn
        self._factory = conn_factory
        self._clock = clock
        self._interval = interval_seconds or load_settings().expiry_sweep_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _purge_due(self) -> bool:
        row = self._conn.execute("select last_run_epoch from maintenance_runs where job_name=?", (PURGE_JOB,)).fetchone()
        return row is None or int(self._clock.now().timestamp()) - row["last_run_epoch"] >= PURGE_EVERY.total_seconds()

    def run_once(self) -> MaintenanceResult:
        expired = expire_due_holds(self._conn, self._clock).expired
        purged = 0
        if self._purge_due():
            now = self._clock.now()
            purged = purge_option_sets(self._conn, now - RETENTION)
            with immediate(self._conn):
                self._conn.execute(
                    "insert into maintenance_runs (job_name, last_run_at, last_run_epoch) values (?,?,?) "
                    "on conflict(job_name) do update set last_run_at=excluded.last_run_at, "
                    "last_run_epoch=excluded.last_run_epoch",
                    (PURGE_JOB, now.isoformat(), int(now.timestamp())),
                )
        return MaintenanceResult(expired=expired, purged_option_sets=purged)

    def start(self) -> None:
        """Prototype loop for a single process.

        SQLite connections cannot cross threads, so the loop opens its own through ``conn_factory``.
        """
        if self._factory is None:
            raise RuntimeError("start() needs conn_factory so the background thread owns its connection")
        self.run_once()  # sweep at startup, as the plan requires
        factory = self._factory

        def loop() -> None:
            worker = MaintenanceRunner(factory(), self._clock, self._interval)
            try:
                while not self._stop.wait(self._interval):
                    worker.run_once()
            finally:
                worker._conn.close()

        self._thread = threading.Thread(target=loop, name="setuhaul-maintenance", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
