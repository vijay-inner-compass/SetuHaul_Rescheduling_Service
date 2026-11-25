"""Send queued driver notices (notification_outbox, recipient DRIVER) to the driver's bound Telegram chat."""

# Lazy annotations.
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from setuhaul.auth import telegram
from setuhaul.auth.onboarding import driver_eligible
from setuhaul.clock import Clock
from setuhaul.db import TimestampError, parse_operational_timestamp
from setuhaul.notifications import DeliveryResult, _mark

# A notice older than this is not sent: it would be about an offer that has long lapsed, and turning delivery on must not flush a backlog.
NOTICE_MAX_AGE = timedelta(minutes=30)
# The waits after each failed send: five attempts over about ten minutes, so a short outage loses nothing and a retried
# offer still arrives within its 15-minute hold.
RETRY_DELAYS = (timedelta(seconds=30), timedelta(minutes=1), timedelta(minutes=3), timedelta(minutes=5))
_LOG = logging.getLogger(__name__)


def _record(conn: sqlite3.Connection, outbox_id: int, status: str, attempts: int, error: str | None, now: datetime) -> None:
    """Write the outcome, waiting out a busy database: a sent notice left unmarked would be sent again."""
    for left in range(4, -1, -1):
        try:
            _mark(conn, outbox_id, status, attempts, error, now)
            return
        except sqlite3.OperationalError:
            conn.rollback()
            if not left:
                raise
            time.sleep(0.2)


def render_notice(payload: dict[str, object]) -> str:
    # The notice's own text when it has one, otherwise a short generic line.
    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return text
    return f"Update on your booking ({payload.get('kind', 'notice')}). Message me for details."


def send_driver_notifications(conn: sqlite3.Connection, clock: Clock, client: telegram.TelegramClient, limit: int = 100,
                              max_age: timedelta = NOTICE_MAX_AGE,
                              not_before: dict[int, datetime] | None = None) -> DeliveryResult:
    """One pass over PENDING/FAILED driver notices. A driver with no active Telegram binding keeps theirs queued.

    ``not_before`` carries the retry waits from one pass to the next; without it every failed notice is due at once.
    """
    not_before = {} if not_before is None else not_before
    # Drivers with an earlier notice still waiting: their later ones are held so messages arrive in order.
    waiting: set[str] = set()
    max_attempts = len(RETRY_DELAYS) + 1
    # Only drivers with an active binding are selected, so a backlog for unbound drivers cannot starve the batch.
    rows = conn.execute(
        "select o.* from notification_outbox o join telegram_identities t on t.driver_id=o.recipient_ref and t.revoked_at is null"
        " where o.recipient_type='DRIVER' and o.status in ('PENDING','FAILED') and o.attempts < ? order by o.outbox_id",
        (max_attempts,)).fetchall()
    today = clock.now().date()
    sent = failed = permanent = 0
    for r in rows:
        # The limit counts notices acted on, so skipped ones cannot fill the batch and starve newer ones.
        if sent + failed + permanent >= limit:
            break
        # A driver who is no longer eligible gets nothing; the notice stays queued, with no attempt used.
        chat_id = telegram.bound_chat(conn, r["recipient_ref"])
        if chat_id is None or not driver_eligible(conn, r["recipient_ref"], today):
            continue
        # A row whose time cannot be read is parked: left in place it would stop every notice behind it, for good.
        try:
            age = clock.now() - parse_operational_timestamp(r["created_at"])
        except (TimestampError, ValueError):
            _record(conn, r["outbox_id"], "PERMANENT_FAILURE", r["attempts"], "BAD_TIMESTAMP", clock.now())
            permanent += 1
            continue
        if age > max_age:
            _record(conn, r["outbox_id"], "PERMANENT_FAILURE", r["attempts"], "TOO_OLD", clock.now())
            permanent += 1
            continue
        if r["recipient_ref"] in waiting:
            continue
        due = not_before.get(r["outbox_id"])
        if due is not None and clock.now() < due:
            waiting.add(r["recipient_ref"])
            continue
        attempts = r["attempts"] + 1
        try:
            payload = json.loads(r["payload_json"])
        except ValueError:
            # A notice that can never be rendered is parked, not retried, and does not stop the batch.
            _record(conn, r["outbox_id"], "PERMANENT_FAILURE", attempts, "MALFORMED_PAYLOAD", clock.now())
            permanent += 1
            continue
        try:
            client.send(chat_id, render_notice(payload if isinstance(payload, dict) else {}))
        except telegram.TelegramSendError as exc:
            # Retried until the attempts run out, like the other notification senders.
            status = "PERMANENT_FAILURE" if attempts >= max_attempts else "FAILED"
            _record(conn, r["outbox_id"], status, attempts, str(exc)[:200], clock.now())
            permanent += status == "PERMANENT_FAILURE"
            failed += status == "FAILED"
            if status == "FAILED":
                not_before[r["outbox_id"]] = clock.now() + RETRY_DELAYS[attempts - 1]
                waiting.add(r["recipient_ref"])
            continue
        _record(conn, r["outbox_id"], "SENT", attempts, None, clock.now())
        sent += 1
    return DeliveryResult(sent=sent, failed=failed, permanent_failures=permanent)


class DriverNoticeRunner:
    """Sends queued driver notices every few seconds on its own thread. One server process only, or messages repeat."""

    def __init__(self, clock: Clock, client: telegram.TelegramClient, conn_factory: Callable[[], sqlite3.Connection],
                 interval_s: float = 5.0) -> None:
        self._clock = clock
        self._client = client
        # SQLite connections cannot cross threads, so each pass opens its own.
        self._factory = conn_factory
        self._interval = interval_s
        # When each failed notice may be tried again. Kept in memory: after a restart they are due at once.
        self._not_before: dict[int, datetime] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> DeliveryResult | None:
        """One pass; None when it failed. A failure never stops the loop."""
        try:
            conn = self._factory()
            try:
                return send_driver_notifications(conn, self._clock, self._client, not_before=self._not_before)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - a locked database or a client fault is retried on the next pass
            # Only the kind of error: a message or traceback could quote the request URL, which holds the bot token.
            _LOG.error("driver notice delivery failed: %s", type(exc).__name__)
            return None

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                self.run_once()
                # Sleeps for the interval but wakes at once when stop() is called.
                self._stop.wait(self._interval)

        self._thread = threading.Thread(target=loop, name="setuhaul-driver-notices", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=15)
