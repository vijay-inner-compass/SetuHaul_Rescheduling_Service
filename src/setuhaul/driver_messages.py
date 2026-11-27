"""Operations writing to a driver at any time, and sending again a message that did not reach the phone."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import json
import sqlite3
from typing import Any

from redis import Redis

from setuhaul import chat_loads
from setuhaul.auth import audit
from setuhaul.auth.authz import facility_clause
from setuhaul.auth.users import User
from setuhaul.clock import Clock
from setuhaul.config import load_settings
from setuhaul.db import immediate
from setuhaul.events import queue_driver_notice
from setuhaul.facility_change import _thread_of
from setuhaul.identity import AuthError
from setuhaul.memory import ConversationMemoryStore


def _load_in_scope(conn: sqlite3.Connection, user: User, driver_id: str) -> str | None:
    """The driver's load the user may write about: bound for their facility, unfinished ones and newest first."""
    clause, params = facility_clause(user, "s.destination_facility_id")
    row = conn.execute(
        f"select s.shipment_id from shipments s where s.driver_id=? and {clause}"
        " order by (s.current_status in ('COMPLETED','CANCELLED')), julianday(s.updated_at) desc, s.shipment_id limit 1",
        (driver_id, *params)).fetchone()
    return None if row is None else str(row[0])


def send(conn: sqlite3.Connection, *, actor: User, driver_id: str, text: str, clock: Clock, redis: Redis) -> bool:
    """Put the message in the driver's chat and queue it for their phone. True if it was queued for the phone."""
    shipment_id = _load_in_scope(conn, actor, driver_id)
    if actor.status != "ACTIVE" or shipment_id is None:
        raise AuthError("DRIVER_NOT_FOUND")
    thread_id = _thread_of(conn, driver_id)
    if thread_id is None:
        raise AuthError("NO_CHAT")
    now = clock.now()
    with chat_loads.recording(conn, thread_id, shipment_id):
        # Through the cache, so the chat model sees what the driver was told.
        ConversationMemoryStore(conn, redis, clock, load_settings()).append(driver_id, thread_id, "OPERATIONS", text)
    conn.commit()
    queued = queue_driver_notice(conn, driver_id, shipment_id, text, now)
    audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action="DRIVER_MESSAGED", target_type="driver",
                 target_id=driver_id, outcome="OK", ip=None, detail={"shipment_id": shipment_id}, clock=clock)
    conn.commit()
    return queued


def undelivered(conn: sqlite3.Connection, user: User, limit: int = 200) -> dict[str, list[dict[str, Any]]]:
    """Messages that failed to reach a phone, by driver: those still retrying and those given up on."""
    clause, params = facility_clause(user, "s.destination_facility_id")
    rows = conn.execute(
        "select o.outbox_id, o.recipient_ref, o.status, o.payload_json from notification_outbox o"
        " join appointment_events e on e.event_id=o.event_id join shipments s on s.shipment_id=e.shipment_id"
        f" where o.recipient_type='DRIVER' and o.status in ('FAILED','PERMANENT_FAILURE') and {clause}"
        " order by o.outbox_id desc limit ?", (*params, limit)).fetchall()
    out: dict[str, list[dict[str, Any]]] = {}
    for r in reversed(rows):
        try:
            text = str(json.loads(r["payload_json"]).get("text") or "Booking update")
        except (ValueError, AttributeError):
            continue
        out.setdefault(r["recipient_ref"], []).append(
            {"outbox_id": r["outbox_id"], "text": text, "gave_up": r["status"] == "PERMANENT_FAILURE"})
    return out


def resend(conn: sqlite3.Connection, *, actor: User, outbox_id: int, clock: Clock) -> None:
    """Queue again a message the service gave up on, with its attempts and age starting over."""
    clause, params = facility_clause(actor, "s.destination_facility_id")
    with immediate(conn):
        row = conn.execute(
            "select o.status, o.recipient_ref from notification_outbox o join appointment_events e on e.event_id=o.event_id"
            f" join shipments s on s.shipment_id=e.shipment_id where o.outbox_id=? and o.recipient_type='DRIVER' and {clause}",
            (outbox_id, *params)).fetchone()
        if row is None or actor.status != "ACTIVE":
            raise AuthError("MESSAGE_NOT_FOUND")
        if row["status"] != "PERMANENT_FAILURE":
            raise AuthError("NOT_FAILED")
        conn.execute("update notification_outbox set status='PENDING', attempts=0, last_error=null, created_at=?,"
                     " delivered_at=null where outbox_id=?", (clock.now().isoformat(), outbox_id))
        audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action="DRIVER_MESSAGE_RESENT",
                     target_type="driver", target_id=row["recipient_ref"], outcome="OK", ip=None,
                     detail={"outbox_id": outbox_id}, clock=clock)
