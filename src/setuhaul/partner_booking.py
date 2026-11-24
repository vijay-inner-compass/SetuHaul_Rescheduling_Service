"""Partner-warehouse slot requests: ask a partner by email, book only after their verified approval reply."""

# Lazy annotations.
from __future__ import annotations

# sqlite3 for the Connection type.
import sqlite3

# uuid for the short request reference.
import uuid

# datetime arithmetic for expiry.
from datetime import datetime, timedelta

# Literal types the result status.
from typing import Literal

# pydantic value objects, like the rest of the service layer.
from pydantic import BaseModel, ConfigDict

# The injectable clock.
from setuhaul.clock import Clock

# immediate() takes the write lock up front.
from setuhaul.db import immediate

# Shipments in these states can no longer be moved.
_NOT_RESCHEDULABLE = ("CANCELLED", "COMPLETED", "IN_DOCK")


class PartnerRequestResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["REQUESTED", "ALREADY_PENDING", "NOT_PARTNER_FACILITY", "NO_PARTNER_CONTACT",
                    "SHIPMENT_NOT_RESCHEDULABLE"]
    request_ref: str | None = None


def _new_ref() -> str:
    # PR- plus 8 uppercase hex characters: short enough for a subject line, unique enough per request.
    return "PR-" + uuid.uuid4().hex[:8].upper()


def request_slot(conn: sqlite3.Connection, clock: Clock, *, shipment_id: str, proposed_start: datetime,
                 proposed_end: datetime, ttl_min: int = 120) -> PartnerRequestResult:
    """Record one pending request for a shipment bound for a partner facility. The email is sent later by a job."""
    with immediate(conn):
        ship = conn.execute("select destination_facility_id, current_status from shipments where shipment_id=?",
                            (shipment_id,)).fetchone()
        if ship is None or ship["current_status"] in _NOT_RESCHEDULABLE:
            return PartnerRequestResult(status="SHIPMENT_NOT_RESCHEDULABLE")
        # Only partner facilities are asked by email; our own docks go through the normal hold flow.
        fac = conn.execute("select is_partner from facilities where facility_id=?",
                           (ship["destination_facility_id"],)).fetchone()
        if fac is None or not fac["is_partner"]:
            return PartnerRequestResult(status="NOT_PARTNER_FACILITY")
        # One open request per shipment: a repeat returns the existing one.
        open_ = conn.execute("select request_ref from partner_requests where shipment_id=? and status='PENDING_PARTNER'",
                             (shipment_id,)).fetchone()
        if open_ is not None:
            return PartnerRequestResult(status="ALREADY_PENDING", request_ref=open_["request_ref"])
        # The coordinator first, then any other active contact that has an email address.
        contact = conn.execute(
            "select contact_id from facility_contacts where facility_id=? and active_flag=1 and coalesce(email,'')<>'' "
            "order by contact_role='WAREHOUSE_COORDINATOR' desc, contact_id limit 1", (ship["destination_facility_id"],)).fetchone()
        if contact is None:
            return PartnerRequestResult(status="NO_PARTNER_CONTACT")
        now = clock.now()
        ref = _new_ref()
        conn.execute(
            "insert into partner_requests (request_ref, shipment_id, facility_id, contact_id, proposed_start, proposed_end, "
            "status, email_status, email_attempts, created_at, expires_at) values (?,?,?,?,?,?,?,?,?,?,?)",
            (ref, shipment_id, ship["destination_facility_id"], contact["contact_id"], proposed_start.isoformat(),
             proposed_end.isoformat(), "PENDING_PARTNER", "PENDING", 0, now.isoformat(),
             (now + timedelta(minutes=ttl_min)).isoformat()))
        return PartnerRequestResult(status="REQUESTED", request_ref=ref)


def expire_partner_requests(conn: sqlite3.Connection, clock: Clock) -> int:
    """Mark overdue pending requests EXPIRED. The shipment's original appointment is never touched."""
    with immediate(conn):
        now = clock.now()
        cur = conn.execute(
            "update partner_requests set status='EXPIRED', decided_at=? where status='PENDING_PARTNER' and expires_at<=?",
            (now.isoformat(), now.isoformat()))
        return cur.rowcount


def render_request_email(conn: sqlite3.Connection, request_ref: str) -> tuple[str, str, str]:
    """(to_address, subject, body). No driver phone number or other personal detail goes in the body."""
    row = conn.execute(
        "select r.request_ref, r.proposed_start, r.proposed_end, c.email, c.contact_name, f.facility_name, "
        "s.order_reference from partner_requests r join facility_contacts c using(contact_id) "
        "join facilities f on f.facility_id=r.facility_id join shipments s using(shipment_id) where r.request_ref=?",
        (request_ref,)).fetchone()
    if row is None:
        raise KeyError(request_ref)
    start = datetime.fromisoformat(row["proposed_start"])
    end = datetime.fromisoformat(row["proposed_end"])
    subject = f"SetuHaul slot request [{request_ref}]"
    body = (
        f"Hello {row['contact_name']},\n\n"
        f"SetuHaul asks for a delivery slot at {row['facility_name']} for order {row['order_reference']}:\n"
        f"  {start:%A %d %B %Y}, {start:%H:%M} to {end:%H:%M} (India time)\n\n"
        "Please reply to this email with one word on the first line:\n"
        "  APPROVE   to confirm the slot\n"
        "  DECLINE   if it cannot be done\n\n"
        f"Keep [{request_ref}] in the subject. We book the slot only after your APPROVE reply.\n"
    )
    return row["email"], subject, body
