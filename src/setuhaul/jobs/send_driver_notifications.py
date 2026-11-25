"""Send queued driver notices (notification_outbox, recipient DRIVER) to the driver's bound Telegram chat."""

# Lazy annotations.
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import timedelta

from setuhaul.auth import telegram
from setuhaul.auth.onboarding import driver_eligible
from setuhaul.clock import Clock
from setuhaul.config import load_settings
from setuhaul.db import parse_operational_timestamp
from setuhaul.notifications import DeliveryResult, _mark

# A notice older than this is not sent: it would be about an offer that has long lapsed, and turning delivery on must not flush a backlog.
NOTICE_MAX_AGE = timedelta(minutes=30)
_LOG = logging.getLogger(__name__)


def render_notice(payload: dict[str, object]) -> str:
    # The notice's own text when it has one, otherwise a short generic line.
    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return text
    return f"Update on your booking ({payload.get('kind', 'notice')}). Message me for details."


def send_driver_notifications(conn: sqlite3.Connection, clock: Clock, client: telegram.TelegramClient, limit: int = 100,
                              max_age: timedelta = NOTICE_MAX_AGE) -> DeliveryResult:
    """One pass over PENDING/FAILED driver notices. A driver with no active Telegram binding keeps theirs queued."""
    max_attempts = load_settings().notification_max_attempts
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
        if clock.now() - parse_operational_timestamp(r["created_at"]) > max_age:
            _mark(conn, r["outbox_id"], "PERMANENT_FAILURE", r["attempts"], "TOO_OLD", clock.now())
            permanent += 1
            continue
        attempts = r["attempts"] + 1
        try:
            payload = json.loads(r["payload_json"])
        except ValueError:
            # A notice that can never be rendered is parked, not retried, and does not stop the batch.
            _mark(conn, r["outbox_id"], "PERMANENT_FAILURE", attempts, "MALFORMED_PAYLOAD", clock.now())
            permanent += 1
            continue
        try:
            client.send(chat_id, render_notice(payload if isinstance(payload, dict) else {}))
        except telegram.TelegramSendError as exc:
            # Retried until the attempts run out, like the other notification senders.
            status = "PERMANENT_FAILURE" if attempts >= max_attempts else "FAILED"
            _mark(conn, r["outbox_id"], status, attempts, str(exc)[:200], clock.now())
            permanent += status == "PERMANENT_FAILURE"
            failed += status == "FAILED"
            continue
        _mark(conn, r["outbox_id"], "SENT", attempts, None, clock.now())
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
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> DeliveryResult | None:
        """One pass; None when it failed. A failure never stops the loop."""
        try:
            conn = self._factory()
            try:
                return send_driver_notifications(conn, self._clock, self._client)
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
