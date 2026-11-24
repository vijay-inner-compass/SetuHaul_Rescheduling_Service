"""Send queued driver notices (notification_outbox, recipient DRIVER) to the driver's bound Telegram chat."""

# Lazy annotations.
from __future__ import annotations

import json
import sqlite3

from setuhaul.auth import telegram
from setuhaul.auth.onboarding import driver_eligible
from setuhaul.clock import Clock
from setuhaul.config import load_settings
from setuhaul.notifications import DeliveryResult, _mark


def render_notice(payload: dict[str, object]) -> str:
    # The notice's own text when it has one, otherwise a short generic line.
    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return text
    return f"Update on your booking ({payload.get('kind', 'notice')}). Message me for details."


def send_driver_notifications(conn: sqlite3.Connection, clock: Clock, client: telegram.TelegramClient, limit: int = 100) -> DeliveryResult:
    """One pass over PENDING/FAILED driver notices. A driver with no active Telegram binding keeps theirs queued."""
    max_attempts = load_settings().notification_max_attempts
    # Only drivers with an active binding are selected, so a backlog for unbound drivers cannot starve the batch.
    rows = conn.execute(
        "select o.* from notification_outbox o join telegram_identities t on t.driver_id=o.recipient_ref and t.revoked_at is null"
        " where o.recipient_type='DRIVER' and o.status in ('PENDING','FAILED') and o.attempts < ? order by o.outbox_id limit ?",
        (max_attempts, limit)).fetchall()
    today = clock.now().date()
    sent = failed = permanent = 0
    for r in rows:
        # A driver who is no longer eligible gets nothing; the notice stays queued, with no attempt used.
        chat_id = telegram.bound_chat(conn, r["recipient_ref"])
        if chat_id is None or not driver_eligible(conn, r["recipient_ref"], today):
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
