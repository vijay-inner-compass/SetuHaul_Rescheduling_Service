"""Hold expiry sweep and the prototype maintenance runner.

Owner: this job (plus lazy expiry inside ``acquire_hold``). In production it runs from an external
scheduler; the single-process prototype wires ``MaintenanceRunner.start`` into the FastAPI lifespan
in Phase 11. Every operation is idempotent, so several runners are safe.
"""

# Lazy annotations.
from __future__ import annotations

import logging

# sqlite3 for the Connection type.
import sqlite3

# threading runs the background loop.
import threading

# Callable types the connection factory.
from collections.abc import Callable

# dataclass for result values.
from dataclasses import dataclass

# timedelta for the purge interval.
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypeVar

# The transactional expiry of one hold (releases the slot, writes history, queues the notice).
from setuhaul import facility_change, gate, retention, round_clock
from setuhaul.booking import expire_hold_in_txn

# The injectable clock.
from setuhaul.clock import Clock

# Settings give the sweep interval.
from setuhaul.config import load_settings

# immediate(): a BEGIN IMMEDIATE transaction.
from setuhaul.db import immediate
from setuhaul.escalation import ingest_fallback_file

# Old option sets are purged after their retention period.
from setuhaul.observability import log_failure
from setuhaul.option_history import RETENTION, purge_option_sets

# The name under which the purge's last run time is stored in maintenance_runs.
PURGE_JOB = "purge_option_sets"
# The purge runs at most once a day.
PURGE_EVERY = timedelta(hours=24)


# What one expiry pass did.
@dataclass(frozen=True)
class ExpiryBatchResult:
    expired: int
    appointment_ids: tuple[str, ...]


# What one full maintenance pass did.
@dataclass(frozen=True)
class MaintenanceResult:
    expired: int
    purged_option_sets: int


def expire_due_holds(conn: sqlite3.Connection, clock: Clock, limit: int = 100) -> ExpiryBatchResult:
    """Expire up to ``limit`` holds whose TTL has elapsed (``expires_at <= now``, inclusive)."""
    # One reading of "now" for the whole pass, so every hold is judged against the same moment.
    now = clock.now()
    # Find overdue holds, oldest expiry first, capped at `limit` so a backlog cannot make one pass run forever.
    # expires_epoch is a plain integer, which makes the comparison exact and index-friendly.
    due = [
        r["appointment_id"]
        for r in conn.execute(
            "select a.appointment_id from appointments a join appointment_hold_meta m using(appointment_id) "
            "where a.appointment_status='HELD' and m.expires_epoch <= ? order by m.expires_epoch, a.appointment_id limit ?",
            (int(now.timestamp()), limit),
        )
    ]
    # Ids actually expired by this pass.
    done: list[str] = []
    for appointment_id in due:
        with immediate(conn):  # one transaction per hold; re-checks status, so duplicates are no-ops
            # Returns False if another runner (or the driver confirming) got there first; then nothing is counted.
            if expire_hold_in_txn(conn, appointment_id, now):
                done.append(appointment_id)
    return ExpiryBatchResult(expired=len(done), appointment_ids=tuple(done))


_T = TypeVar("_T")


class MaintenanceRunner:
    def __init__(
        self,
        conn: sqlite3.Connection | None,
        clock: Clock,
        interval_seconds: float | None = None,
        conn_factory: Callable[[], sqlite3.Connection] | None = None,
        fallback_path: Path | None = None,
        retention_enabled: bool = False,
        retention_policy: retention.RetentionPolicy | None = None,
    ) -> None:
        # A connection for run_once() (None when the runner is only used to start a background thread).
        self._conn = conn
        # A function that opens a fresh connection; needed because SQLite connections cannot be shared across threads.
        self._factory = conn_factory
        # Where hand-offs are written when the database is down; loaded back by the sweep.
        self._fallback = fallback_path
        self._retention_enabled = retention_enabled
        self._retention_policy = retention_policy or retention.RetentionPolicy()
        self._clock = clock
        # Seconds between sweeps: the argument, or the configured default (30 s).
        self._interval = interval_seconds or load_settings().expiry_sweep_seconds
        # Set to ask the background loop to stop.
        self._stop = threading.Event()
        # The background thread, once started.
        self._thread: threading.Thread | None = None

    def close(self) -> None:
        # Close the connection if this runner owns one.
        if self._conn is not None:
            self._conn.close()

    def _db(self) -> sqlite3.Connection:
        # A clear failure if run_once() is used on a runner built without a connection.
        assert self._conn is not None, "this runner was built without a connection; use start() with a conn_factory"
        return self._conn

    def _job_due(self, job_name: str) -> bool:
        row = self._db().execute("select last_run_epoch from maintenance_runs where job_name=?", (job_name,)).fetchone()
        return row is None or int(self._clock.now().timestamp()) - row["last_run_epoch"] >= PURGE_EVERY.total_seconds()

    def _mark_job(self, job_name: str, now: datetime) -> None:
        with immediate(self._db()):
            self._db().execute(
                "insert into maintenance_runs (job_name, last_run_at, last_run_epoch) values (?,?,?) "
                "on conflict(job_name) do update set last_run_at=excluded.last_run_at, "
                "last_run_epoch=excluded.last_run_epoch",
                (job_name, now.isoformat(), int(now.timestamp())),
            )

    def _run_daily_retention(self, job_name: str, fn: Callable[[], int]) -> int:
        if not self._job_due(job_name):
            return 0
        now = self._clock.now()
        affected = fn()
        self._mark_job(job_name, now)
        return affected

    def _purge_due(self) -> bool:
        # When did the purge last run?
        row = self._db().execute("select last_run_epoch from maintenance_runs where job_name=?", (PURGE_JOB,)).fetchone()
        # Due if it has never run, or at least 24 hours have passed.
        return row is None or int(self._clock.now().timestamp()) - row["last_run_epoch"] >= PURGE_EVERY.total_seconds()

    # One failing step is logged and skipped: the others (and the next pass) still run.
    def _step(self, name: str, work: Callable[[], _T], otherwise: _T) -> _T:
        try:
            return work()
        except Exception as exc:  # noqa: BLE001
            self._db().rollback()
            log_failure(logging.getLogger(__name__), f"maintenance: {name}", exc)
            return otherwise

    def _purge(self) -> int:
        # The housekeeping purge only runs once a day.
        if not self._purge_due():
            return 0
        now = self._clock.now()
        # Delete option sets older than the retention period (30 days).
        purged = purge_option_sets(self._db(), now - RETENTION)
        # Record when the purge ran; "on conflict ... do update" is an upsert keyed by job name.
        with immediate(self._db()):
            self._db().execute(
                "insert into maintenance_runs (job_name, last_run_at, last_run_epoch) values (?,?,?) "
                "on conflict(job_name) do update set last_run_at=excluded.last_run_at, "
                "last_run_epoch=excluded.last_run_epoch",
                (PURGE_JOB, now.isoformat(), int(now.timestamp())),
            )
        return purged

    def run_once(self) -> MaintenanceResult:
        db, clock, fallback = self._db(), self._clock, self._fallback
        # Always expire overdue holds first: that is time-critical.
        expired = self._step("hold expiry", lambda: expire_due_holds(db, clock).expired, 0)
        # Hand-offs written during a database outage join the queue.
        if fallback is not None:
            self._step("fallback hand-offs", lambda: ingest_fallback_file(db, fallback, clock), 0)
        # An approved facility move whose offer just lapsed goes back to where the booking is.
        self._step("facility change follow-up", lambda: facility_change.settle(db, clock), 0)
        # Unused passes expire; a truck still inside after its window is raised with the warehouse.
        self._step("gate sweep", lambda: gate.sweep(db, clock), 0)
        # Creates only what is missing, so a new day appears at the end of the range soon after midnight.
        self._step("slot top-up", lambda: round_clock.top_up(db, clock), 0)
        purged = self._step("option purge", self._purge, 0)
        if self._retention_enabled:
            p = self._retention_policy
            now = clock.now()
            self._step("retention: chat messages", lambda: self._run_daily_retention(
                "purge_chat_messages", lambda: retention.purge_chat_messages(db, now - timedelta(days=p.chat_days), p.batch_size)), 0)
            self._step("retention: gps pings", lambda: self._run_daily_retention(
                "purge_gps_pings", lambda: retention.purge_gps_pings(db, now - timedelta(days=p.gps_days), p.batch_size)), 0)
            self._step("retention: audit log ips", lambda: self._run_daily_retention(
                "purge_audit_log_ips", lambda: retention.purge_audit_log_ips(db, now - timedelta(days=p.audit_ip_days), p.batch_size)), 0)
            self._step("retention: inbound messages", lambda: self._run_daily_retention(
                "purge_inbound_messages", lambda: retention.purge_inbound_messages(db, now - timedelta(days=p.inbound_days), p.batch_size)), 0)
            self._step("retention: model calls", lambda: self._run_daily_retention(
                "purge_model_calls", lambda: retention.purge_model_calls(db, now - timedelta(days=p.model_calls_days), p.batch_size)), 0)
        return MaintenanceResult(expired=expired, purged_option_sets=purged)

    def run_safely(self) -> None:
        """One pass for the background loop: an error (a locked database, say) is logged and the next pass still runs."""
        try:
            self.run_once()
        except Exception as exc:  # noqa: BLE001 - the sweep must outlive any single failure
            log_failure(logging.getLogger(__name__), "maintenance sweep", exc)

    def start(self) -> None:
        """Prototype loop for a single process.

        SQLite connections cannot cross threads, so the loop opens its own through ``conn_factory``.
        """
        # Without a factory the background thread would have to share this thread's connection, which SQLite forbids.
        if self._factory is None:
            raise RuntimeError("start() needs conn_factory so the background thread owns its connection")
        # A throwaway runner for the first sweep, which happens immediately at startup.
        boot = MaintenanceRunner(
            self._factory(), self._clock, self._interval, fallback_path=self._fallback,
            retention_enabled=self._retention_enabled, retention_policy=self._retention_policy,
        )  # opened and closed on this thread
        try:
            boot.run_safely()  # sweep at startup, as the plan requires
        finally:
            boot.close()
        factory, fallback = self._factory, self._fallback

        def loop() -> None:
            # The loop opens its own connection inside its own thread.
            worker = MaintenanceRunner(
                factory(), self._clock, self._interval, fallback_path=fallback,
                retention_enabled=self._retention_enabled, retention_policy=self._retention_policy,
            )
            try:
                # Event.wait(timeout) sleeps for the interval but wakes at once if stop() is called; it returns True then.
                while not self._stop.wait(self._interval):
                    worker.run_safely()
            finally:
                worker.close()

        # daemon=True: the thread never keeps the process alive after the main program exits.
        self._thread = threading.Thread(target=loop, name="setuhaul-maintenance", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        # Ask the loop to finish, then wait up to 5 seconds for the thread to exit.
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
