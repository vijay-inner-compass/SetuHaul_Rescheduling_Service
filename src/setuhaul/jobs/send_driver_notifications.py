"""Send queued driver notices (notification_outbox, recipient DRIVER) to the driver's bound Telegram chat."""

# Lazy annotations.
from __future__ import annotations

import json
import sqlite3

from setuhaul.auth import telegram
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
    rows = conn.execute("select * from notification_outbox where recipient_type='DRIVER' and status in ('PENDING','FAILED')"
                        " and attempts < ? order by outbox_id limit ?", (max_attempts, limit)).fetchall()
    sent = failed = permanent = 0
    for r in rows:
        chat_id = telegram.bound_chat(conn, r["recipient_ref"])
        # Not bound (yet): leave it queued, with no attempt used.
        if chat_id is None:
            continue
        attempts = r["attempts"] + 1
        try:
            client.send(chat_id, render_notice(json.loads(r["payload_json"])))
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
