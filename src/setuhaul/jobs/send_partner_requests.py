"""Send queued partner request emails. At-least-once: a crash after sending may resend, never loses a request."""

# Lazy annotations.
from __future__ import annotations

# logging for per-request failures (no credentials in the text).
import logging

# sqlite3 for the Connection type.
import sqlite3

# uuid for a stable Message-ID.
import uuid

# The injectable clock.
from setuhaul.clock import Clock

# The transport interface and error.
from setuhaul.mail_transport import MailDeliveryError, MailSender, OutgoingMail

# The mail text.
from setuhaul.partner_booking import render_request_email

_LOG = logging.getLogger(__name__)
# After this many failed attempts the request email is marked FAILED and an operator must look at it.
MAX_ATTEMPTS = 5


def send_pending_requests(conn: sqlite3.Connection, clock: Clock, sender: MailSender) -> int:
    """Send every open request whose email is still PENDING. Returns how many were sent."""
    rows = conn.execute("select request_ref, message_id, email_attempts from partner_requests "
                        "where email_status='PENDING' and status='PENDING_PARTNER' order by created_at").fetchall()
    sent = 0
    for row in rows:
        ref = row["request_ref"]
        # Stored first so the Message-ID on the wire is the one we can match replies against.
        message_id = row["message_id"] or f"<{ref.lower()}.{uuid.uuid4().hex[:8]}@setuhaul.local>"
        conn.execute("update partner_requests set message_id=? where request_ref=?", (message_id, ref))
        conn.commit()
        to_addr, subject, body = render_request_email(conn, ref)
        try:
            sender.send(OutgoingMail(to_addr=to_addr, subject=subject, body=body, message_id=message_id))
        except MailDeliveryError as exc:
            attempts = row["email_attempts"] + 1
            conn.execute("update partner_requests set email_attempts=?, last_error=?, email_status=? where request_ref=?",
                         (attempts, str(exc)[:200], "FAILED" if attempts >= MAX_ATTEMPTS else "PENDING", ref))
            conn.commit()
            _LOG.warning("partner request %s email failed (attempt %d): %s", ref, attempts, exc)
            continue
        conn.execute("update partner_requests set email_status='SENT', last_error=null where request_ref=?", (ref,))
        conn.commit()
        sent += 1
    return sent
