"""Moving a load to another facility: the driver asks, the destination warehouse (or an admin) decides.

Approval changes ``shipments.destination_facility_id`` and nothing else in the supplied data; the original facility is kept
on the request. Booking a slot at the new facility is a separate step (``after_approval``).
"""

# Lazy annotations.
from __future__ import annotations

import sqlite3
import uuid
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

from setuhaul.auth import audit
from setuhaul.auth.authz import facility_ids
from setuhaul.auth.users import User
from setuhaul.clock import Clock
from setuhaul.db import immediate, parse_operational_timestamp

REQUEST_LIFE = timedelta(minutes=120)
# A load in these states is no longer ours to move.
NOT_MOVABLE = ("CANCELLED", "COMPLETED", "IN_DOCK")

Status = Literal["REQUESTED", "ALREADY_PENDING", "SAME_FACILITY", "UNKNOWN_FACILITY", "NOT_MOVABLE",
                 "APPROVED", "REJECTED", "NOT_PENDING", "NOT_FOUND"]


class ChangeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Status
    request_id: str | None = None


def expire_due(conn: sqlite3.Connection, clock: Clock) -> int:
    """Close pending requests whose time has passed. The caller owns the transaction."""
    now = clock.now()
    due = [r["request_id"] for r in conn.execute("select request_id, expires_at from facility_change_requests where status='PENDING'")
           if parse_operational_timestamp(r["expires_at"]) <= now]
    for request_id in due:
        conn.execute("update facility_change_requests set status='EXPIRED', decided_at=? where request_id=?", (now.isoformat(), request_id))
    return len(due)


def request_change(conn: sqlite3.Connection, clock: Clock, *, driver_id: str, shipment_id: str, to_facility_id: str) -> ChangeResult:
    with immediate(conn):
        expire_due(conn, clock)
        ship = conn.execute("select driver_id, destination_facility_id, current_status from shipments where shipment_id=?",
                            (shipment_id,)).fetchone()
        # Another driver's load and an unknown one answer alike.
        if ship is None or ship["driver_id"] != driver_id or ship["current_status"] in NOT_MOVABLE:
            return ChangeResult(status="NOT_MOVABLE")
        if conn.execute("select 1 from facilities where facility_id=? and active_flag=1", (to_facility_id,)).fetchone() is None:
            return ChangeResult(status="UNKNOWN_FACILITY")
        open_ = conn.execute("select request_id from facility_change_requests where shipment_id=? and status='PENDING'",
                             (shipment_id,)).fetchone()
        if open_ is not None:
            return ChangeResult(status="ALREADY_PENDING", request_id=open_["request_id"])
        if to_facility_id == ship["destination_facility_id"]:
            return ChangeResult(status="SAME_FACILITY")
        now = clock.now()
        request_id = "FCR-" + uuid.uuid4().hex[:10].upper()
        conn.execute(
            "insert into facility_change_requests(request_id, shipment_id, driver_id, from_facility_id, to_facility_id, status, "
            "created_at, expires_at) values (?,?,?,?,?,'PENDING',?,?)",
            (request_id, shipment_id, driver_id, ship["destination_facility_id"], to_facility_id, now.isoformat(),
             (now + REQUEST_LIFE).isoformat()))
        audit.record(conn, actor_user_id=None, actor_email=None, action="FACILITY_CHANGE_REQUESTED", target_type="shipment",
                     target_id=shipment_id, outcome="OK", ip=None, detail={"driver_id": driver_id, "to": to_facility_id}, clock=clock)
    return ChangeResult(status="REQUESTED", request_id=request_id)


def decide(conn: sqlite3.Connection, clock: Clock, *, actor: User, request_id: str, approve: bool) -> ChangeResult:
    with immediate(conn):
        expire_due(conn, clock)
        row = conn.execute("select * from facility_change_requests where request_id=?", (request_id,)).fetchone()
        scope = facility_ids(actor)
        # Only the destination's own warehouse, or an admin. Anyone else sees "no such request".
        if row is None or actor.status != "ACTIVE" or (scope is not None and row["to_facility_id"] not in scope):
            return ChangeResult(status="NOT_FOUND")
        if row["status"] != "PENDING":
            return ChangeResult(status="NOT_PENDING", request_id=request_id)
        if approve:
            ship = conn.execute("select destination_facility_id, current_status from shipments where shipment_id=?",
                                (row["shipment_id"],)).fetchone()
            # The load may have moved on since it was asked for.
            if ship["current_status"] in NOT_MOVABLE or ship["destination_facility_id"] != row["from_facility_id"]:
                return ChangeResult(status="NOT_MOVABLE", request_id=request_id)
            conn.execute("update shipments set destination_facility_id=? where shipment_id=?", (row["to_facility_id"], row["shipment_id"]))
        status: Status = "APPROVED" if approve else "REJECTED"
        conn.execute("update facility_change_requests set status=?, decided_by=?, decided_at=? where request_id=?",
                     (status, actor.user_id, clock.now().isoformat(), request_id))
        audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action=f"FACILITY_CHANGE_{status}",
                     target_type="shipment", target_id=row["shipment_id"], outcome="OK", ip=None,
                     detail={"request_id": request_id, "to": row["to_facility_id"]}, clock=clock)
    return ChangeResult(status=status, request_id=request_id)
