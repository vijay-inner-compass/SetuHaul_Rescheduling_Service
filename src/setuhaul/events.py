"""Immutable appointment history and the transactional notification outbox.

Both writers take the caller's connection and never commit: they run inside the same transaction
as the state change they describe, so history and outbox can never disagree with the data.
"""

# Lazy annotations.
from __future__ import annotations

# json stores the free-form details and payload as text columns.
import json

# sqlite3 provides the Connection type the writers use.
import sqlite3

# datetime is the timestamp type of every event.
from datetime import datetime

# Any: details and payloads are free-form dictionaries.
from typing import Any


def append_appointment_event(
    conn: sqlite3.Connection,
    appointment_id: str,
    shipment_id: str,
    event_type: str,
    *,
    from_status: str | None,
    to_status: str | None,
    actor_type: str,
    actor_id: str | None,
    reason: str | None,
    occurred_at: datetime,
    details: dict[str, Any] | None = None,
) -> int:
    # One INSERT into the append-only history table. There is deliberately no update or delete function:
    # history is a log, so who did what and when can always be reconstructed.
    cur = conn.execute(
        "insert into appointment_events (appointment_id, shipment_id, event_type, from_status, to_status, "
        "actor_type, actor_id, reason, details_json, occurred_at) values (?,?,?,?,?,?,?,?,?,?)",
        # sort_keys makes the stored JSON identical for identical content, which keeps comparisons and replays stable.
        (appointment_id, shipment_id, event_type, from_status, to_status, actor_type, actor_id, reason,
         json.dumps(details or {}, sort_keys=True), occurred_at.isoformat()),
    )
    # After a successful insert SQLite always reports the new row id; the assert tells the type checker so.
    assert cur.lastrowid is not None
    # The id lets the caller link a notification to exactly this event.
    return cur.lastrowid


def enqueue_notification(
    conn: sqlite3.Connection,
    dedupe_key: str,
    recipient_type: str,
    recipient_ref: str,
    event_id: int,
    payload: dict[str, Any],
    now: datetime,
) -> bool:
    """Queue a notice once per ``dedupe_key``. Returns False if it was already queued."""
    # "insert or ignore" plus a unique index on dedupe_key means a retried request cannot queue the same notice twice.
    cur = conn.execute(
        "insert or ignore into notification_outbox (dedupe_key, recipient_type, recipient_ref, event_id, "
        "payload_json, created_at) values (?,?,?,?,?,?)",
        (dedupe_key, recipient_type, recipient_ref, event_id, json.dumps(payload, sort_keys=True), now.isoformat()),
    )
    # rowcount is 1 when a row was inserted and 0 when the key already existed (the "ignore" case).
    return cur.rowcount == 1


def queue_driver_notice(conn: sqlite3.Connection, driver_id: str, shipment_id: str, text: str, now: datetime) -> bool:
    """Queue a message for the driver's phone: one written outside their turn reaches Telegram only through the outbox."""
    # The outbox hangs off an appointment event; a load with no appointment at all has nothing to hang it on.
    row = conn.execute("select appointment_id from appointments where shipment_id=? order by is_current desc, rowid desc limit 1",
                       (shipment_id,)).fetchone()
    if row is None:
        return False
    event_id = append_appointment_event(conn, row[0], shipment_id, "DRIVER_NOTICE", from_status=None, to_status=None,
                                        actor_type="SYSTEM", actor_id=None, reason=None, occurred_at=now)
    queued = enqueue_notification(conn, f"DRIVER_NOTICE:{event_id}", "DRIVER", driver_id, event_id, {"kind": "notice", "text": text}, now)
    # Called outside any wider transaction: commit, so the write lock is not left open.
    conn.commit()
    return queued


def get_appointment_history(conn: sqlite3.Connection, shipment_id: str) -> list[dict[str, Any]]:
    """Who changed what, when and why, oldest first, for one shipment."""
    # Join to appointments to include the slot each event concerned; order by event_id for oldest-first.
    rows = conn.execute(
        "select e.event_id, e.occurred_at, e.appointment_id, e.event_type, e.from_status, e.to_status, "
        "e.actor_type, e.actor_id, e.reason, e.details_json, a.slot_id from appointment_events e "
        "join appointments a using(appointment_id) where e.shipment_id=? order by e.event_id",
        (shipment_id,),
    )
    # Turn each row into a dict and decode the stored JSON text back into a real "details" object.
    return [{**dict(r), "details": json.loads(r["details_json"])} for r in rows]
