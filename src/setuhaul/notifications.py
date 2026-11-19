"""Downstream notifications: enqueued inside the booking transaction, delivered outside it.

Delivery is at-least-once with a stable ``dedupe_key`` the receiver can use to drop repeats.
A delivery failure never touches the appointment: it is recorded on the outbox row only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from setuhaul.clock import Clock
from setuhaul.config import load_settings
from setuhaul.events import enqueue_notification


class PermanentDeliveryError(Exception):
    """The receiver rejected the notice for good; do not retry."""


@dataclass(frozen=True)
class Notice:
    outbox_id: int
    dedupe_key: str
    recipient_type: str
    recipient_ref: str
    payload: dict[str, Any]


Sender = Callable[[Notice], None]


@dataclass(frozen=True)
class DeliveryResult:
    sent: int
    failed: int
    permanent_failures: int


def enqueue_downstream(
    conn: sqlite3.Connection, event_id: int, kind: str, shipment_id: str, appointment_id: str,
    payload: dict[str, Any], now: datetime,
) -> int:
    """Queue planner, consignee and operations notices. Caller owns the transaction."""
    s = conn.execute(
        "select destination_facility_id, customer_name from shipments where shipment_id=?", (shipment_id,)
    ).fetchone()
    facility = s["destination_facility_id"]

    def contact(role: str) -> str:
        r = conn.execute(
            "select contact_name from facility_contacts where facility_id=? and contact_role=? and active_flag=1 "
            "order by contact_id limit 1", (facility, role),
        ).fetchone()
        return r["contact_name"] if r else f"ROLE:{role}"

    recipients = [
        ("WAREHOUSE_PLANNER", contact("WAREHOUSE_COORDINATOR")),
        ("CONSIGNEE", s["customer_name"]),
        ("OPERATIONS", contact("SHIFT_MANAGER")),
    ]
    queued = 0
    for rtype, ref in recipients:
        body = {"kind": kind, "shipment_id": shipment_id, "appointment_id": appointment_id,
                "facility_id": facility, **payload}
        if enqueue_notification(conn, f"{kind}:{appointment_id}:{rtype}:{ref}", rtype, ref, event_id, body, now):
            queued += 1
    return queued


def _mark(conn: sqlite3.Connection, outbox_id: int, status: str, attempts: int, error: str | None, now: datetime) -> None:
    conn.execute(
        "update notification_outbox set status=?, attempts=?, last_error=?, "
        "delivered_at=case when ?='SENT' then ? else delivered_at end where outbox_id=?",
        (status, attempts, error, status, now.isoformat(), outbox_id),
    )
    conn.commit()


def deliver_pending_notifications(
    conn: sqlite3.Connection, clock: Clock, sender: Sender, limit: int = 100
) -> DeliveryResult:
    """Send PENDING and retryable FAILED notices. Runs outside any booking transaction."""
    import json

    max_attempts = load_settings().notification_max_attempts
    rows = conn.execute(
        "select * from notification_outbox where status in ('PENDING','FAILED') and attempts < ? "
        "order by outbox_id limit ?", (max_attempts, limit),
    ).fetchall()
    sent = failed = permanent = 0
    for r in rows:
        notice = Notice(r["outbox_id"], r["dedupe_key"], r["recipient_type"], r["recipient_ref"],
                        json.loads(r["payload_json"]))
        attempts = r["attempts"] + 1
        try:
            sender(notice)
        except PermanentDeliveryError as exc:
            _mark(conn, r["outbox_id"], "PERMANENT_FAILURE", attempts, str(exc), clock.now())
            permanent += 1
            continue
        except Exception as exc:  # transient: keep for retry until attempts run out  # noqa: BLE001 - unknown sender failures are transient by definition; retried then marked permanent
            status = "PERMANENT_FAILURE" if attempts >= max_attempts else "FAILED"
            _mark(conn, r["outbox_id"], status, attempts, str(exc), clock.now())
            permanent += status == "PERMANENT_FAILURE"
            failed += status == "FAILED"
            continue
        _mark(conn, r["outbox_id"], "SENT", attempts, None, clock.now())
        sent += 1
    return DeliveryResult(sent=sent, failed=failed, permanent_failures=permanent)


def recipient_status(conn: sqlite3.Connection, event_id: int) -> list[tuple[str, str, str]]:
    return [
        (r["recipient_type"], r["recipient_ref"], r["status"])
        for r in conn.execute(
            "select recipient_type, recipient_ref, status from notification_outbox where event_id=? order by outbox_id",
            (event_id,),
        )
    ]
