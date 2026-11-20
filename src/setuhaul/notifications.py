"""Downstream notifications: enqueued inside the booking transaction, delivered outside it.

Delivery is at-least-once with a stable ``dedupe_key`` the receiver can use to drop repeats.
A delivery failure never touches the appointment: it is recorded on the outbox row only.
"""

# Lazy annotations.
from __future__ import annotations

# sqlite3 for the Connection type.
import sqlite3

# Callable types the sender function.
from collections.abc import Callable

# dataclass for value objects.
from dataclasses import dataclass

# datetime for timestamps.
from datetime import datetime

# Any for free-form payloads.
from typing import Any

# The injectable clock.
from setuhaul.clock import Clock

# The retry limit comes from settings.
from setuhaul.config import load_settings

# The low-level insert into the outbox table.
from setuhaul.events import enqueue_notification


# Raised by a sender when the receiver says "never send this again" (for example a closed account). It stops retries.
class PermanentDeliveryError(Exception):
    """The receiver rejected the notice for good; do not retry."""


# One queued message as the sender sees it.
@dataclass(frozen=True)
class Notice:
    outbox_id: int
    # A stable key the receiver can use to discard a duplicate delivery.
    dedupe_key: str
    recipient_type: str
    recipient_ref: str
    payload: dict[str, Any]


# A sender is any function that takes a Notice and either returns (success) or raises (failure).
Sender = Callable[[Notice], None]


# Counts returned after a delivery pass.
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
    # The shipment's destination facility and customer decide who gets told.
    s = conn.execute(
        "select destination_facility_id, customer_name from shipments where shipment_id=?", (shipment_id,)
    ).fetchone()
    facility = s["destination_facility_id"]

    def contact(role: str) -> str:
        # The first active contact for that role at the facility.
        r = conn.execute(
            "select contact_name from facility_contacts where facility_id=? and contact_role=? and active_flag=1 "
            "order by contact_id limit 1", (facility, role),
        ).fetchone()
        # If nobody is listed, queue to the role itself ("ROLE:SHIFT_MANAGER") so the notice is not silently lost.
        return r["contact_name"] if r else f"ROLE:{role}"

    # Three audiences: the dock planner, the consignee (customer) and the operations shift manager.
    recipients = [
        ("WAREHOUSE_PLANNER", contact("WAREHOUSE_COORDINATOR")),
        ("CONSIGNEE", s["customer_name"]),
        ("OPERATIONS", contact("SHIFT_MANAGER")),
    ]
    queued = 0
    for rtype, ref in recipients:
        # The same facts go to everyone; the recipient fields differ.
        body = {"kind": kind, "shipment_id": shipment_id, "appointment_id": appointment_id,
                "facility_id": facility, **payload}
        # The dedupe key includes kind, appointment and recipient, so a retried booking never queues a second copy.
        if enqueue_notification(conn, f"{kind}:{appointment_id}:{rtype}:{ref}", rtype, ref, event_id, body, now):
            queued += 1
    # How many new notices were actually queued (duplicates are not counted).
    return queued


def _mark(conn: sqlite3.Connection, outbox_id: int, status: str, attempts: int, error: str | None, now: datetime) -> None:
    # Record the outcome on the outbox row. delivered_at is set only when the new status is SENT; otherwise it is kept.
    conn.execute(
        "update notification_outbox set status=?, attempts=?, last_error=?, "
        "delivered_at=case when ?='SENT' then ? else delivered_at end where outbox_id=?",
        (status, attempts, error, status, now.isoformat(), outbox_id),
    )
    # Commit per notice so one failure cannot roll back the record of the others.
    conn.commit()


def deliver_pending_notifications(
    conn: sqlite3.Connection, clock: Clock, sender: Sender, limit: int = 100
) -> DeliveryResult:
    """Send PENDING and retryable FAILED notices. Runs outside any booking transaction."""
    import json

    # The retry ceiling (3 attempts by default).
    max_attempts = load_settings().notification_max_attempts
    # Oldest first; only notices that are not yet sent and still have attempts left.
    rows = conn.execute(
        "select * from notification_outbox where status in ('PENDING','FAILED') and attempts < ? "
        "order by outbox_id limit ?", (max_attempts, limit),
    ).fetchall()
    # Running totals; three names assigned the same starting value.
    sent = failed = permanent = 0
    for r in rows:
        # Rebuild the Notice object from the stored row.
        notice = Notice(r["outbox_id"], r["dedupe_key"], r["recipient_type"], r["recipient_ref"],
                        json.loads(r["payload_json"]))
        # This will be the Nth attempt.
        attempts = r["attempts"] + 1
        try:
            # The actual delivery. The sender does the network call (or, in the demo, just records it).
            sender(notice)
        except PermanentDeliveryError as exc:
            # The receiver refused for good: mark it so it is never retried.
            _mark(conn, r["outbox_id"], "PERMANENT_FAILURE", attempts, str(exc), clock.now())
            permanent += 1
            continue
        except Exception as exc:  # transient: keep for retry until attempts run out  # noqa: BLE001 - unknown sender failures are transient by definition; retried then marked permanent
            # Any other error is assumed temporary: FAILED (retry later) until the attempt limit, then permanent.
            status = "PERMANENT_FAILURE" if attempts >= max_attempts else "FAILED"
            _mark(conn, r["outbox_id"], status, attempts, str(exc), clock.now())
            # Booleans add as 0 or 1, so this counts without an if.
            permanent += status == "PERMANENT_FAILURE"
            failed += status == "FAILED"
            continue
        # No exception: delivered.
        _mark(conn, r["outbox_id"], "SENT", attempts, None, clock.now())
        sent += 1
    return DeliveryResult(sent=sent, failed=failed, permanent_failures=permanent)


def recipient_status(conn: sqlite3.Connection, event_id: int) -> list[tuple[str, str, str]]:
    # For one appointment event: who was told, and whether delivery succeeded (type, reference, status).
    return [
        (r["recipient_type"], r["recipient_ref"], r["status"])
        for r in conn.execute(
            "select recipient_type, recipient_ref, status from notification_outbox where event_id=? order by outbox_id",
            (event_id,),
        )
    ]
