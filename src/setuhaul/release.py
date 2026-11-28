"""Freeing a booking from the warehouse side, shared by the board cross, dock blocking and driver deletion.

The shipment is kept: only its appointment ends. History and notices are written in the caller's transaction; the driver is
told afterwards, because queuing their message commits on its own.
"""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from redis import Redis

from setuhaul.auth import audit
from setuhaul.auth.authz import facility_ids
from setuhaul.auth.telegram import bound_chat
from setuhaul.auth.users import User
from setuhaul.booking import release_hold_in_txn
from setuhaul.clock import Clock
from setuhaul.config import load_settings
from setuhaul.db import IST, immediate, parse_operational_timestamp
from setuhaul.events import append_appointment_event
from setuhaul.facility_change import _tell, _thread_of
from setuhaul.identity import AuthError
from setuhaul.memory import ConversationMemoryStore
from setuhaul.notifications import enqueue_downstream

# Why a booking was freed; stored as its cancellation reason.
Cause = Literal["SLOT_FREED", "DOCK_BLOCKED", "DRIVER_DELETED"]
# Only a booking that has not started unloading may be freed.
FREEABLE = ("CONFIRMED", "PENDING_CONFIRMATION")


@dataclass(frozen=True)
class Booking:
    appointment_id: str
    shipment_id: str
    driver_id: str
    driver_name: str
    order_reference: str
    facility_id: str
    dock_id: str
    dock_code: str
    slot_id: str
    start: datetime
    end: datetime
    status: str


def booking(conn: sqlite3.Connection, appointment_id: str) -> Booking | None:
    row = conn.execute(
        "select a.appointment_id, a.shipment_id, s.driver_id, d.driver_name, s.order_reference, sl.facility_id, sl.dock_id,"
        " k.dock_code, sl.slot_id, sl.slot_start_ts, sl.slot_end_ts, a.appointment_status from appointments a"
        " join shipments s using(shipment_id) join drivers d on d.driver_id=s.driver_id"
        " join appointment_slots sl on sl.slot_id=a.slot_id join docks k on k.dock_id=sl.dock_id where a.appointment_id=?",
        (appointment_id,)).fetchone()
    if row is None:
        return None
    start, end = (parse_operational_timestamp(row[c]).astimezone(IST) for c in ("slot_start_ts", "slot_end_ts"))
    return Booking(row["appointment_id"], row["shipment_id"], row["driver_id"], row["driver_name"], row["order_reference"],
                   row["facility_id"], row["dock_id"], row["dock_code"], row["slot_id"], start, end, row["appointment_status"])


def truck_inside(conn: sqlite3.Connection, shipment_id: str) -> bool:
    """The truck is at the facility now: through the gate by pass, or recorded there by the supplied data."""
    return conn.execute(
        "select 1 from shipments where shipment_id=? and current_status in ('AT_GATE','WAITING','IN_DOCK') union all "
        "select 1 from truck_visits t join visit_shipments v using(truck_visit_id) where v.shipment_id=?"
        " and t.status in ('CHECKED_IN','OPERATIONS_COMPLETE')", (shipment_id, shipment_id)).fetchone() is not None


def not_freeable(conn: sqlite3.Connection, found: Booking) -> str | None:
    """Why this booking may not be freed, or None if it may."""
    if found.status == "IN_PROGRESS":
        return "UNLOADING"
    if found.status not in FREEABLE:
        return "NOT_BOOKED"
    return "TRUCK_ON_SITE" if truck_inside(conn, found.shipment_id) else None


def free_in_txn(conn: sqlite3.Connection, found: Booking, cause: Cause, actor: User, clock: Clock, *,
                announce: bool = True) -> None:
    """End the booking and any hold of its shipment. Call inside a transaction, after `not_freeable` said yes.

    `announce` queues the planner, customer and operations notices; a move announces itself instead. The gate pass is left
    alone: the gate already refuses a shipment with no appointment, and a rebooking moves the pass with it.
    """
    now = clock.now()
    stamp = now.isoformat()
    for held in conn.execute("select appointment_id from appointments where shipment_id=? and appointment_status='HELD'",
                             (found.shipment_id,)).fetchall():
        release_hold_in_txn(conn, held["appointment_id"], found.shipment_id, "SYSTEM", now, "WAREHOUSE", actor.user_id, cause)
    conn.execute("update appointments set appointment_status='CANCELLED', is_current=0, cancelled_at=?, cancellation_reason=?,"
                 " updated_at=? where appointment_id=?", (stamp, cause, stamp, found.appointment_id))
    event_id = append_appointment_event(
        conn, found.appointment_id, found.shipment_id, "APPOINTMENT_CANCELLED", from_status=found.status,
        to_status="CANCELLED", actor_type="WAREHOUSE", actor_id=actor.user_id, reason=cause, occurred_at=now)
    if announce:
        enqueue_downstream(conn, event_id, "APPOINTMENT_CANCELLED", found.shipment_id, found.appointment_id,
                           {"cause": cause}, now)
    audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action="BOOKING_FREED", target_type="shipment",
                 target_id=found.shipment_id, outcome="OK", ip=None,
                 detail={"appointment_id": found.appointment_id, "cause": cause}, clock=clock)


def free_slot(conn: sqlite3.Connection, *, actor: User, appointment_id: str, clock: Clock) -> Booking:
    """The dock board cross: free one booking that has not started."""
    with immediate(conn):
        found = booking(conn, appointment_id)
        scope = facility_ids(actor)
        # Out of scope, unknown and partner bookings all look the same.
        if (found is None or actor.status != "ACTIVE" or (scope is not None and found.facility_id not in scope)
                or conn.execute("select 1 from facilities where facility_id=? and is_partner=1", (found.facility_id,)).fetchone()):
            raise AuthError("APPOINTMENT_NOT_FOUND")
        refusal = not_freeable(conn, found)
        if refusal:
            raise AuthError(refusal)


        free_in_txn(conn, found, "SLOT_FREED", actor, clock)
    return found


def tell_driver(conn: sqlite3.Connection, clock: Clock, telegram_on: bool, found: Booking, key: str, *,
                redis: Redis | None = None, **fields: str) -> bool:
    """Put the message in the driver's chat and queue it for their phone. True if it can reach the phone.

    Call after the transaction that changed the booking. With `redis` the cached conversation gets the message too, so the
    chat model sees it. A failure is reported as "not reached", never raised: the change is already saved.
    """
    try:
        memory = None if redis is None else ConversationMemoryStore(conn, redis, clock, load_settings())
        _tell(conn, clock, memory, found.driver_id, _thread_of(conn, found.driver_id), found.shipment_id, key, happened_at=None,
              **fields)
        conn.commit()
        return telegram_on and bound_chat(conn, found.driver_id) is not None
    # Broad on purpose: staff must learn who was not told, whatever went wrong.
    except Exception:
        logging.getLogger(__name__).exception("telling the driver failed for appointment %s", found.appointment_id)
        conn.rollback()
        return False
