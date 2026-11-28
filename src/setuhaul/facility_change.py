"""Moving a load to another facility: the driver asks, the destination warehouse (or an admin) decides.

Approval changes ``shipments.destination_facility_id`` and nothing else in the supplied data; the original facility is kept
on the request. Booking a slot at the new facility is a separate step (``after_approval``).
"""

# Lazy annotations.
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from setuhaul import chat_loads, passes
from setuhaul.allocation import rank_options
from setuhaul.auth import audit
from setuhaul.auth.authz import facility_ids
from setuhaul.auth.users import User
from setuhaul.booking import acquire_hold, make_hold_checker, release_hold, release_hold_in_txn
from setuhaul.capacity import build_facility_state
from setuhaul.clock import Clock
from setuhaul.config import load_settings
from setuhaul.db import IST, immediate, parse_operational_timestamp
from setuhaul.escalation import HandoffRequest, SqlEscalationSink
from setuhaul.eta import load_eta_records
from setuhaul.events import append_appointment_event, queue_driver_notice
from setuhaul.feasibility import build_context, search_feasible_slots
from setuhaul.gps import gps_arrival_floor
from setuhaul.language import say
from setuhaul.memory import ConversationMemoryStore, HoldRef
from setuhaul.notifications import enqueue_downstream
from setuhaul.option_history import record_option_set
from setuhaul.partner_booking import request_slot
from setuhaul.schemas import BaselineType, EtaEvidence

REQUEST_LIFE = timedelta(minutes=120)
# A load in these states is no longer ours to move.
NOT_MOVABLE = ("CANCELLED", "COMPLETED", "IN_DOCK", "AT_GATE", "WAITING")

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
        ship = conn.execute("select driver_id, destination_facility_id, current_status from v_driver_shipments where shipment_id=?",
                            (shipment_id,)).fetchone()
        # Another driver's load and an unknown one answer alike.
        if ship is None or ship["driver_id"] != driver_id or ship["current_status"] in NOT_MOVABLE or passes.on_site(conn, shipment_id):
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
            if (ship["current_status"] in NOT_MOVABLE or ship["destination_facility_id"] != row["from_facility_id"]
                    or passes.on_site(conn, row["shipment_id"])):
                return ChangeResult(status="NOT_MOVABLE", request_id=request_id)
            orig = conn.execute(
                "select appointment_id, appointment_status from appointments where shipment_id=? and is_current=1"
                " and appointment_status in ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')",
                (row["shipment_id"],),
            ).fetchone()
            if orig is not None and orig["appointment_status"] == "IN_PROGRESS":
                return ChangeResult(status="NOT_MOVABLE", request_id=request_id)
            now = clock.now()
            stamp = now.isoformat()
            for held in conn.execute("select appointment_id from appointments where shipment_id=? and appointment_status='HELD'",
                                     (row["shipment_id"],)).fetchall():
                release_hold_in_txn(conn, held["appointment_id"], row["shipment_id"], "SYSTEM", now,
                                    "WAREHOUSE", actor.user_id, "FACILITY_CHANGED")
            if orig is not None:
                conn.execute(
                    "update appointments set appointment_status='CANCELLED', is_current=0, cancelled_at=?,"
                    " cancellation_reason='FACILITY_CHANGED', updated_at=? where appointment_id=?",
                    (stamp, stamp, orig["appointment_id"]),
                )
                event_id = append_appointment_event(
                    conn, orig["appointment_id"], row["shipment_id"], "APPOINTMENT_CANCELLED",
                    from_status=orig["appointment_status"], to_status="CANCELLED",
                    actor_type="WAREHOUSE", actor_id=actor.user_id, reason="FACILITY_CHANGED", occurred_at=now,
                )
                enqueue_downstream(conn, event_id, "APPOINTMENT_CANCELLED", row["shipment_id"], orig["appointment_id"],
                                   {"cause": "FACILITY_CHANGED", "from_facility_id": row["from_facility_id"],
                                    "to_facility_id": row["to_facility_id"]}, now)
                audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action="BOOKING_FREED",
                             target_type="shipment", target_id=row["shipment_id"], outcome="OK", ip=None,
                             detail={"appointment_id": orig["appointment_id"], "cause": "FACILITY_CHANGED"}, clock=clock)
            conn.execute("update shipments set destination_facility_id=? where shipment_id=?",
                         (row["to_facility_id"], row["shipment_id"]))

            conn.execute("update partner_requests set status='EXPIRED', decided_at=? where shipment_id=? and status='PENDING_PARTNER'",
                         (stamp, row["shipment_id"]))
            passes.move_warehouse(conn, row["shipment_id"], clock)
        status: Status = "APPROVED" if approve else "REJECTED"
        conn.execute("update facility_change_requests set status=?, decided_by=?, decided_at=? where request_id=?",
                     (status, actor.user_id, clock.now().isoformat(), request_id))
        if approve:
            # An earlier approved move that was still waiting for a booking: stop waiting without rewriting history.
            earlier = conn.execute("select request_id, from_facility_id from facility_change_requests "
                                   "where shipment_id=? and follow_up='WAITING'", (row["shipment_id"],)).fetchone()
            if earlier is not None:


                conn.execute("update facility_change_requests set reason='SUPERSEDED', follow_up=NULL "
                             "where request_id=?", (earlier["request_id"],))
        audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action=f"FACILITY_CHANGE_{status}",
                     target_type="shipment", target_id=row["shipment_id"], outcome="OK", ip=None,
                     detail={"request_id": request_id, "to": row["to_facility_id"]}, clock=clock)
    return ChangeResult(status=status, request_id=request_id)


# ---- after approval -------------------------------------------------------------------------------------
# A hold made on the driver's behalf has to last long enough for them to see the message (the usual hold is 180 s).
APPROVAL_HOLD_SECONDS = 15 * 60

Outcome = Literal["HELD", "PARTNER_REQUESTED", "NO_SLOT"]


def _thread_of(conn: sqlite3.Connection, driver_id: str) -> str | None:
    row = conn.execute("select thread_id from chat_threads where driver_id=? and thread_status<>'CLOSED' order by opened_at desc, rowid desc limit 1",
                       (driver_id,)).fetchone()
    return row["thread_id"] if row else None


def _language(conn: sqlite3.Connection, driver_id: str, thread_id: str | None) -> str:
    # The language the driver has been writing in; English until one is established.
    row = conn.execute("select state_json from conversation_state where driver_id=? and thread_id=?", (driver_id, thread_id)).fetchone()
    return (json.loads(row["state_json"]).get("language") if row else None) or "en"


def _tell(conn: sqlite3.Connection, clock: Clock, memory: ConversationMemoryStore | None, driver_id: str, thread_id: str | None,
          shipment_id: str, key: str, *, happened_at: datetime | None = None, **fields: str) -> None:
    """Put a message in the driver's chat, in their language, and queue it for their phone."""
    text = say(key, _language(conn, driver_id, thread_id), **fields)
    now = clock.now()
    with chat_loads.recording(conn, thread_id, shipment_id):
        if thread_id is not None and memory is not None:
            memory.append(driver_id, thread_id, "OPERATIONS", text)
        elif thread_id is not None:
            # Callers without the cache (the sweeps) write the durable log; the cache is rebuilt from it.
            conn.execute("insert into chat_messages (chat_message_id, thread_id, sender_type, message_text, message_ts) "
                         "values (?,?, 'OPERATIONS', ?, ?)", ("MSG-" + uuid.uuid4().hex[:12].upper(), thread_id, text, now.isoformat()))
    queue_driver_notice(conn, driver_id, shipment_id, text, now, happened_at)


def _release_live_holds(conn: sqlite3.Connection, clock: Clock, driver_id: str, shipment_id: str) -> None:
    """Cancel every hold the load still has, whichever conversation made it."""
    rows = conn.execute(
        "select a.appointment_id, m.thread_id from appointments a join appointment_hold_meta m using(appointment_id) "
        "where a.shipment_id=? and a.appointment_status='HELD' and m.driver_id=?", (shipment_id, driver_id)).fetchall()
    for row in rows:
        release_hold(conn, driver_id, row["thread_id"], row["appointment_id"], "SYSTEM", clock)


def _facility_name(conn: sqlite3.Connection, facility_id: str) -> str:
    return str(conn.execute("select facility_name from facilities where facility_id=?", (facility_id,)).fetchone()[0])


def _revert(conn: sqlite3.Connection, clock: Clock, request_id: str, reason: str, message_key: str, *,
            case_summary: str | None = None, memory: ConversationMemoryStore | None = None,
            happened_at: datetime | None = None) -> bool:
    """Mark an approved move as unbooked without reverting the destination warehouse: the old slot is already freed."""
    with immediate(conn):
        req = conn.execute("select * from facility_change_requests where request_id=? and status='APPROVED' "
                           "and coalesce(follow_up,'')<>'BOOKED' and reason is null", (request_id,)).fetchone()
        if req is None:
            return False
        # Keep destination_facility_id at to_facility_id; old slot was freed on approval so a new slot can be allocated here.

        conn.execute("update facility_change_requests set reason=?, follow_up=NULL where request_id=?", (reason, request_id))
        audit.record(conn, actor_user_id=None, actor_email=None, action="FACILITY_CHANGE_UNBOOKED", target_type="shipment",
                     target_id=req["shipment_id"], outcome="OK", ip=None, detail={"request_id": request_id, "reason": reason}, clock=clock)
    driver_id, shipment_id = req["driver_id"], req["shipment_id"]
    _release_live_holds(conn, clock, driver_id, shipment_id)
    thread_id = _thread_of(conn, driver_id)
    if case_summary is not None and thread_id is not None:
        SqlEscalationSink(conn, clock).record(HandoffRequest(
            reason_code="NO_SLOT", driver_id=driver_id, thread_id=thread_id, shipment_id=shipment_id,
            summary=case_summary, created_at=clock.now()))
    _tell(conn, clock, memory, driver_id, thread_id, shipment_id, message_key, happened_at=happened_at,
          facility=_facility_name(conn, req["to_facility_id"]), origin=_facility_name(conn, req["from_facility_id"]))
    return True


def abandon(conn: sqlite3.Connection, clock: Clock, request_id: str, reason: str) -> None:
    """The follow-up to an approval failed: undo the move, tell the driver and ask a person to look."""
    _revert(conn, clock, request_id, reason, "move_no_slot",
            case_summary="A facility move was approved but the follow-up failed.")


def _lapsed_at(conn: sqlite3.Connection, clock: Clock, req: sqlite3.Row) -> datetime:
    """When the move stopped being possible: the last hold or partner request ran out. Never later than now."""
    ends = [parse_operational_timestamp(req["decided_at"])]
    ends += [parse_operational_timestamp(r[0]) for r in conn.execute(
        "select m.expires_at from appointment_hold_meta m join appointments a using(appointment_id) where a.shipment_id=?",
        (req["shipment_id"],))]
    ends += [parse_operational_timestamp(r[0]) for r in conn.execute(
        "select coalesce(decided_at, expires_at) from partner_requests where shipment_id=?", (req["shipment_id"],))]
    return min(clock.now(), max(ends))


def settle(conn: sqlite3.Connection, clock: Clock, driver_id: str | None = None) -> int:
    """Close approved moves that are waiting: booked ones stay; ones that can no longer be booked go back."""
    rows = conn.execute("select * from facility_change_requests where status='APPROVED' and follow_up='WAITING'").fetchall()
    reverted = 0
    for req in rows:
        if driver_id is not None and req["driver_id"] != driver_id:
            continue
        shipment_id = req["shipment_id"]
        booked = conn.execute(
            "select 1 from appointments a join appointment_slots s on s.slot_id=a.slot_id join docks d on d.dock_id=s.dock_id "
            "where a.shipment_id=? and a.is_current=1 and a.appointment_status in ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS') "
            "and d.facility_id=?", (shipment_id, req["to_facility_id"])).fetchone()
        if booked is not None:
            with immediate(conn):
                conn.execute("update facility_change_requests set follow_up='BOOKED' where request_id=?", (req["request_id"],))
            continue
        # Still possible: a hold the driver can answer, or a partner who has not replied.
        held = conn.execute(
            "select 1 from appointments a join appointment_hold_meta m using(appointment_id) where a.shipment_id=? "
            "and a.appointment_status='HELD' and m.expires_epoch > ?", (shipment_id, int(clock.now().timestamp()))).fetchone()
        asked = conn.execute("select 1 from partner_requests where shipment_id=? and status='PENDING_PARTNER'", (shipment_id,)).fetchone()
        if held is None and asked is None:
            reverted += _revert(conn, clock, req["request_id"], "NOT_CONFIRMED", "move_lapsed",
                                happened_at=_lapsed_at(conn, clock, req))
    return reverted


def after_approval(conn: sqlite3.Connection, redis: Any, clock: Clock, request_id: str) -> Outcome:
    """Offer the driver something at the new facility, or put the load back. Tells the driver either way."""
    req = conn.execute("select * from facility_change_requests where request_id=? and status='APPROVED'", (request_id,)).fetchone()
    if req is None:
        raise ValueError(f"no approved request {request_id}")
    driver_id, shipment_id = req["driver_id"], req["shipment_id"]
    new = conn.execute("select facility_name, is_partner from facilities where facility_id=?", (req["to_facility_id"],)).fetchone()
    memory = ConversationMemoryStore(conn, redis, clock, load_settings(), hold_checker=make_hold_checker(conn, clock))
    thread_id = _thread_of(conn, driver_id) or memory.open_thread(driver_id, shipment_id)
    now = clock.now()
    planned, current = load_eta_records(conn, shipment_id)
    record = current or planned

    def waiting() -> None:
        with immediate(conn):
            conn.execute("update facility_change_requests set follow_up='WAITING' where request_id=?", (request_id,))

    def give_back(reason: str) -> Outcome:
        _revert(conn, clock, request_id, reason, "move_no_slot", memory=memory,
                case_summary=f"A move to {new['facility_name']} was approved but nothing could be offered there.")
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
        _release_live_holds(conn, clock, driver_id, shipment_id)
        memory.save_structured_state(driver_id, thread_id, memory.load(driver_id, thread_id).state.model_copy(update={"hold": None}))
        waiting()
        _tell(conn, clock, memory, driver_id, thread_id, shipment_id, "move_partner",
              facility=new["facility_name"], time=f"{start:%H:%M}", date=f"{start:%d %b}")
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
        waiting()
        _tell(conn, clock, memory, driver_id, thread_id, shipment_id, "move_held", facility=new["facility_name"], dock=slot["dock_code"],
              start=f"{start:%H:%M}", end=f"{end:%H:%M}", date=f"{start:%d %b %Y}", until=f"{held.expires_at.astimezone(IST):%H:%M}")
        return "HELD"
    return give_back("NO_SLOT")


def tell_rejected(conn: sqlite3.Connection, redis: Any, clock: Clock, request_id: str) -> None:
    """Tell the driver a request was refused; nothing about their booking changed."""
    req = conn.execute("select driver_id, shipment_id, from_facility_id, to_facility_id from facility_change_requests where request_id=?",
                       (request_id,)).fetchone()
    memory = ConversationMemoryStore(conn, redis, clock, load_settings(), hold_checker=make_hold_checker(conn, clock))
    thread_id = _thread_of(conn, req["driver_id"]) or memory.open_thread(req["driver_id"], req["shipment_id"])
    _tell(conn, clock, memory, req["driver_id"], thread_id, req["shipment_id"], "move_rejected",
          facility=_facility_name(conn, req["to_facility_id"]), origin=_facility_name(conn, req["from_facility_id"]))
