"""Moving a load to another facility: the driver asks, the destination warehouse (or an admin) decides.

Approval changes ``shipments.destination_facility_id`` and nothing else in the supplied data; the original facility is kept
on the request. Booking a slot at the new facility is a separate step (``after_approval``).
"""

# Lazy annotations.
from __future__ import annotations

import sqlite3
import uuid
from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from setuhaul.allocation import rank_options
from setuhaul.auth import audit
from setuhaul.auth.authz import facility_ids
from setuhaul.auth.users import User
from setuhaul.booking import acquire_hold, make_hold_checker, release_hold
from setuhaul.capacity import build_facility_state
from setuhaul.clock import Clock
from setuhaul.config import load_settings
from setuhaul.db import IST, immediate, parse_operational_timestamp
from setuhaul.escalation import HandoffRequest, SqlEscalationSink
from setuhaul.eta import load_eta_records
from setuhaul.feasibility import build_context, search_feasible_slots
from setuhaul.gps import gps_arrival_floor
from setuhaul.memory import ConversationMemoryStore, HoldRef
from setuhaul.option_history import record_option_set
from setuhaul.partner_booking import request_slot
from setuhaul.schemas import BaselineType, EtaEvidence

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


# ---- after approval -------------------------------------------------------------------------------------
# A hold made on the driver's behalf has to last long enough for them to see the message (the usual hold is 180 s).
APPROVAL_HOLD_SECONDS = 15 * 60

Outcome = Literal["HELD", "PARTNER_REQUESTED", "NO_SLOT"]


def _release_live_holds(conn: sqlite3.Connection, clock: Clock, memory: ConversationMemoryStore, driver_id: str, thread_id: str,
                        shipment_id: str) -> None:
    """Cancel every hold the load still has, whichever conversation made it, and forget the remembered one."""
    rows = conn.execute(
        "select a.appointment_id, m.thread_id from appointments a join appointment_hold_meta m using(appointment_id) "
        "where a.shipment_id=? and a.appointment_status='HELD' and m.driver_id=?", (shipment_id, driver_id)).fetchall()
    for row in rows:
        release_hold(conn, driver_id, row["thread_id"], row["appointment_id"], "SYSTEM", clock)
    memory.save_structured_state(driver_id, thread_id, memory.load(driver_id, thread_id).state.model_copy(update={"hold": None}))


def after_approval(conn: sqlite3.Connection, redis: Any, clock: Clock, request_id: str) -> Outcome:
    """Offer the driver something at the new facility, or put the load back. Tells the driver either way."""
    req = conn.execute("select * from facility_change_requests where request_id=? and status='APPROVED'", (request_id,)).fetchone()
    if req is None:
        raise ValueError(f"no approved request {request_id}")
    driver_id, shipment_id = req["driver_id"], req["shipment_id"]
    new = conn.execute("select facility_name, is_partner from facilities where facility_id=?", (req["to_facility_id"],)).fetchone()
    old_name = conn.execute("select facility_name from facilities where facility_id=?", (req["from_facility_id"],)).fetchone()[0]
    memory = ConversationMemoryStore(conn, redis, clock, load_settings(), hold_checker=make_hold_checker(conn, clock))
    row = conn.execute("select thread_id from chat_threads where driver_id=? and thread_status<>'CLOSED' order by opened_at desc, rowid desc limit 1",
                       (driver_id,)).fetchone()
    thread_id = row["thread_id"] if row else memory.open_thread(driver_id, shipment_id)
    now = clock.now()
    planned, current = load_eta_records(conn, shipment_id)
    record = current or planned

    def tell(text: str) -> None:
        memory.append(driver_id, thread_id, "OPERATIONS", text)

    def give_back(reason: str) -> Outcome:
        # Never leave a load pointing at a site where nothing could be offered.
        with immediate(conn):
            conn.execute("update shipments set destination_facility_id=? where shipment_id=?", (req["from_facility_id"], shipment_id))
            conn.execute("update facility_change_requests set status='REJECTED', reason=? where request_id=?", (reason, request_id))
            audit.record(conn, actor_user_id=None, actor_email=None, action="FACILITY_CHANGE_REVERTED", target_type="shipment",
                         target_id=shipment_id, outcome="OK", ip=None, detail={"request_id": request_id, "reason": reason}, clock=clock)
        SqlEscalationSink(conn, clock).record(HandoffRequest(
            reason_code="NO_SLOT", driver_id=driver_id, thread_id=thread_id, shipment_id=shipment_id,
            summary=f"A move to {new['facility_name']} was approved but nothing could be offered there.", created_at=now))
        tell(f"There is no free slot at {new['facility_name']}, so your load is still going to {old_name}. "
             "Your booking there is unchanged. The operations team will help.")
        return "NO_SLOT"

    if record is None:
        return give_back("NO_ETA")
    if new["is_partner"]:
        # The partner decides by email: ask for the next full hour after the arrival, and promise nothing.
        arrival = max(record.eta, now).astimezone(IST)
        start = (arrival + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        asked = request_slot(conn, clock, shipment_id=shipment_id, proposed_start=start, proposed_end=start + timedelta(hours=1))
        if asked.status not in ("REQUESTED", "ALREADY_PENDING"):
            return give_back("NO_PARTNER_REQUEST")
        # A slot still held at the old facility must go: the driver's next "Yes" would otherwise book it.
        _release_live_holds(conn, clock, memory, driver_id, thread_id, shipment_id)
        tell(f"The move to {new['facility_name']} is approved. I have asked them for {start:%H:%M} on {start:%d %b}. "
             "It is NOT booked until they reply.")
        return "PARTNER_REQUESTED"

    eta = EtaEvidence(eta=record.eta, source="DRIVER_DECLARED", confidence="MEDIUM", baseline_type=BaselineType.CURRENT_ETA)
    # The same rule the chat search applies: a slot the truck cannot reach from where it is now is never offered.
    floor = gps_arrival_floor(conn, shipment_id, clock, load_settings())
    if floor is not None and floor > eta.eta:
        eta = eta.model_copy(update={"eta": floor, "source": "GPS_ASSISTED"})
    # Leave room for the whole hold: confirming re-checks reachability, and the truck keeps its distance while the driver reads.
    context = build_context(conn, shipment_id, eta, now, eta_buffer_minutes=APPROVAL_HOLD_SECONDS // 60)
    state = build_facility_state(conn, context.facility_id, now)
    result = search_feasible_slots(context, state)
    if result.status != "OK":
        return give_back("NO_SLOT")
    shown = record_option_set(conn, context, thread_id, rank_options(result.feasible, limit=3), state, clock)
    for item in shown.items:
        held = acquire_hold(conn, driver_id, thread_id, shipment_id, shown.option_set_id, item.rank, clock,
                            ttl_seconds=APPROVAL_HOLD_SECONDS, supersede=True)
        if held.status != "HELD" or held.appointment_id is None or held.expires_at is None:
            continue
        slot = conn.execute("select s.slot_start_ts, s.slot_end_ts, d.dock_code from appointment_slots s join docks d using(dock_id) "
                            "where s.slot_id=?", (held.slot_id,)).fetchone()
        start, end = parse_operational_timestamp(slot["slot_start_ts"]), parse_operational_timestamp(slot["slot_end_ts"])
        memory_state = memory.load(driver_id, thread_id).state
        # "SYSTEM" is no driver message, so the driver's next "Yes" counts as a later message and may confirm.
        memory.save_structured_state(driver_id, thread_id, memory_state.model_copy(update={
            "selected_shipment_id": shipment_id, "latest_option_set_id": shown.option_set_id, "offered_slot_ids": (),
            "hold": HoldRef(appointment_id=held.appointment_id, expires_at=held.expires_at, created_message_id="SYSTEM")}))
        tell(f"The move to {new['facility_name']} is approved. I am holding {start:%H:%M}-{end:%H:%M} at dock {slot['dock_code']} "
             f"on {start:%d %b %Y} until {held.expires_at.astimezone(IST):%H:%M}. It is not booked yet. "
             "Reply Yes to confirm, or No for the next slot.")
        return "HELD"
    return give_back("NO_SLOT")


def tell_rejected(conn: sqlite3.Connection, redis: Any, clock: Clock, request_id: str) -> None:
    """Tell the driver a request was refused; nothing about their booking changed."""
    req = conn.execute(
        "select q.driver_id, q.shipment_id, t.facility_name as destination, f.facility_name as origin from facility_change_requests q "
        "join facilities t on t.facility_id=q.to_facility_id join facilities f on f.facility_id=q.from_facility_id where q.request_id=?",
        (request_id,)).fetchone()
    memory = ConversationMemoryStore(conn, redis, clock, load_settings(), hold_checker=make_hold_checker(conn, clock))
    row = conn.execute("select thread_id from chat_threads where driver_id=? and thread_status<>'CLOSED' order by opened_at desc, rowid desc limit 1",
                       (req["driver_id"],)).fetchone()
    thread_id = row["thread_id"] if row else memory.open_thread(req["driver_id"], req["shipment_id"])
    memory.append(req["driver_id"], thread_id, "OPERATIONS",
                  f"The move to {req['destination']} was not approved. Your load is still going to {req['origin']}; your booking is unchanged.")
