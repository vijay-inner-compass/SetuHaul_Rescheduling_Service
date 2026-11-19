"""Immutable appointment history and the transactional notification outbox.

Both writers take the caller's connection and never commit: they run inside the same transaction
as the state change they describe, so history and outbox can never disagree with the data.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
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
    cur = conn.execute(
        "insert into appointment_events (appointment_id, shipment_id, event_type, from_status, to_status, "
        "actor_type, actor_id, reason, details_json, occurred_at) values (?,?,?,?,?,?,?,?,?,?)",
        (appointment_id, shipment_id, event_type, from_status, to_status, actor_type, actor_id, reason,
         json.dumps(details or {}, sort_keys=True), occurred_at.isoformat()),
    )
    assert cur.lastrowid is not None
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
    cur = conn.execute(
        "insert or ignore into notification_outbox (dedupe_key, recipient_type, recipient_ref, event_id, "
        "payload_json, created_at) values (?,?,?,?,?,?)",
        (dedupe_key, recipient_type, recipient_ref, event_id, json.dumps(payload, sort_keys=True), now.isoformat()),
    )
    return cur.rowcount == 1
