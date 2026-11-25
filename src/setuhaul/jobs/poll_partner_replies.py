"""Read the inbox and act on partner replies. A mail is marked seen only after it was handled, so a crash re-reads it safely."""

# Lazy annotations.
from __future__ import annotations

# logging for per-mail failures.
import logging

# sqlite3 for the Connection type.
import sqlite3

# The injectable clock.
from setuhaul.clock import Clock

# The reader interface.
from setuhaul.mail_transport import MailReader

# The reply logic. Imported by name so tests can wrap it.
from setuhaul.partner_booking import handle_reply

_LOG = logging.getLogger(__name__)


def poll_partner_replies(conn: sqlite3.Connection, clock: Clock, reader: MailReader, *, require_auth: bool = False,
                         auth_serv_id: str = "mx.google.com") -> int:
    """Handle every unseen mail once. Returns how many were handled and marked seen."""
    handled = 0
    try:
        for mail in reader.fetch_unseen():
            try:
                # Already dealt with (the inbox is searched by subject, not by the Seen flag): nothing to do.
                if conn.execute("select 1 from partner_inbound where message_id=?", (mail.message_id,)).fetchone():
                    reader.mark_seen(mail.message_id)
                    continue
                handle_reply(conn, clock, message_id=mail.message_id, from_addr=mail.from_addr, subject=mail.subject,
                             in_reply_to=mail.in_reply_to, body=mail.body, authentication_results=mail.authentication_results, require_auth=require_auth,
                             auth_serv_id=auth_serv_id)
                # partner_inbound's primary key makes a re-read after a crash here a harmless no-op.
                reader.mark_seen(mail.message_id)
                handled += 1
            except Exception:  # noqa: BLE001 - one bad mail must not block the rest; it stays unseen and is retried
                _LOG.exception("partner reply %s could not be handled", mail.message_id)
    finally:
        reader.close()
    return handled
