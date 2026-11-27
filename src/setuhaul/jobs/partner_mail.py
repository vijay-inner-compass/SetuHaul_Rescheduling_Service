"""Background runner for partner-warehouse email: send requests, poll replies, expire overdue requests.

Runs on its own daemon thread so a slow or unreachable mail server never delays hold expiry or Telegram driver notices.
Each step is isolated so one failure (SMTP down, IMAP down, locked DB) does not stop the others.
"""

# Lazy annotations.
from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from setuhaul.clock import Clock
from setuhaul.jobs.expire_holds import record_runner_success
from setuhaul.jobs.expire_partner_requests import run_expiry_once
from setuhaul.jobs.poll_partner_replies import poll_partner_replies
from setuhaul.jobs.send_partner_requests import send_pending_requests
from setuhaul.mail_transport import MailReader, MailSender
from setuhaul.observability import log_failure

_LOG = logging.getLogger(__name__)
_T = TypeVar("_T")


@dataclass(frozen=True)
class PartnerMailResult:
    sent: int
    replies_handled: int
    expired: int


class PartnerMailRunner:
    """Runs the three partner-mail jobs on a dedicated background thread (one server process only)."""

    def __init__(
        self,
        conn: sqlite3.Connection | None,
        clock: Clock,
        sender: MailSender,
        reader: MailReader,
        *,
        interval_seconds: float = 30.0,
        conn_factory: Callable[[], sqlite3.Connection] | None = None,
        require_auth: bool = False,
        auth_serv_id: str = "mx.google.com",
    ) -> None:
        # Connection for run_once() (None when started only as a background thread).
        self._conn = conn
        # Opens a thread-local connection; SQLite connections cannot cross threads.
        self._factory = conn_factory
        self._clock = clock
        self._sender = sender
        self._reader = reader
        self._interval = interval_seconds
        self._require_auth = require_auth
        self._auth_serv_id = auth_serv_id
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()

    def _db(self) -> sqlite3.Connection:
        assert self._conn is not None, "this runner was built without a connection; use start() with a conn_factory"
        return self._conn

    # One failing step is logged and skipped: the remaining steps and the next pass still run.
    def _step(self, name: str, work: Callable[[], _T], otherwise: _T) -> _T:
        try:
            return work()
        except Exception as exc:  # noqa: BLE001
            self._db().rollback()
            log_failure(_LOG, f"partner mail: {name}", exc)
            return otherwise

    def run_once(self) -> PartnerMailResult:
        db, clock = self._db(), self._clock
        sent = self._step("send requests", lambda: send_pending_requests(db, clock, self._sender), 0)
        handled = self._step(
            "poll replies",
            lambda: poll_partner_replies(
                db, clock, self._reader, require_auth=self._require_auth, auth_serv_id=self._auth_serv_id
            ),
            0,
        )
        expired = self._step("expire requests", lambda: run_expiry_once(db, clock), 0)
        self._step("record success", lambda: record_runner_success(db, "partner_mail_runner", clock.now()), None)
        return PartnerMailResult(sent=sent, replies_handled=handled, expired=expired)

    def run_safely(self) -> None:
        """One pass for the background loop: a top-level error is logged and the loop continues."""
        try:
            self.run_once()
        except Exception as exc:  # noqa: BLE001 - the loop must outlive any single failure
            log_failure(_LOG, "partner mail sweep", exc)

    def start(self) -> None:
        """Start the background thread. Does not run a blocking pass on the caller's thread so mail outages cannot slow start-up."""
        if self._factory is None:
            raise RuntimeError("start() needs conn_factory so the background thread owns its connection")
        factory = self._factory

        def loop() -> None:
            worker = PartnerMailRunner(
                factory(),
                self._clock,
                self._sender,
                self._reader,
                interval_seconds=self._interval,
                require_auth=self._require_auth,
                auth_serv_id=self._auth_serv_id,
            )
            try:
                while not self._stop.is_set():
                    worker.run_safely()
                    # Sleeps for the interval but wakes immediately when stop() is called.
                    self._stop.wait(self._interval)
            finally:
                worker.close()

        self._thread = threading.Thread(target=loop, name="setuhaul-partner-mail", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
