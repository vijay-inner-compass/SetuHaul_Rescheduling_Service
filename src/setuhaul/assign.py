"""Operations books the slot a case is resolved with: the old booking ends and the new one is confirmed.

Runs inside the transaction that resolves the case, so the case is closed only if the booking really changed.
"""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime, time, timedelta

from setuhaul import passes, release
from setuhaul.auth import audit
from setuhaul.auth.authz import facility_ids
from setuhaul.auth.users import User
from setuhaul.booking import _expire_due_for, release_hold_in_txn
from setuhaul.capacity import build_facility_state
from setuhaul.clock import Clock
from setuhaul.config import load_settings
from setuhaul.db import IST, parse_operational_timestamp
from setuhaul.eta import load_eta_records
from setuhaul.events import append_appointment_event
from setuhaul.feasibility import build_context, evaluate_slot
from setuhaul.gps import gps_arrival_floor
from setuhaul.identity import AuthError
from setuhaul.notifications import enqueue_downstream
from setuhaul.schemas import BaselineType, EtaEvidence

# Appointments that keep a slot busy.
_TAKEN = "('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS','HELD')"


class Refused(AuthError):
    """The booking rules refuse the slot; `reasons` are their codes."""

    def __init__(self, reasons: tuple[str, ...]) -> None:
        super().__init__("LOAD_DOES_NOT_FIT")
        self.reasons = reasons


# What the person at the screen is told for each refusal.
MESSAGES = {
    "SLOT_REQUIRED": "Choose the slot to book for this driver.",
    "SLOT_NOT_ALLOWED": "A slot is booked only with the outcome \"Assigned a slot\".",
    "SHIPMENT_NOT_FOUND": "This case has no load of yours to book a slot for. Choose another outcome.",
    "PARTNER_FACILITY": "Partner warehouses are booked by email, not here.",
    "SHIPMENT_FINISHED": "That load is already finished or cancelled.",
    "ORIGINAL_IN_PROGRESS": "The truck is already at the facility, so its booking cannot be moved.",
    "SLOT_NOT_AVAILABLE": "That slot has been taken or has passed. Reload and choose another.",
    "LOAD_DOES_NOT_FIT": "This load cannot use that slot",
}
# Why the booking rules refused a slot.
_WHY = {
    "DOCK_TYPE_MISMATCH": "the dock type does not match", "REFRIGERATION_REQUIRED": "the dock is not refrigerated",
    "HEAVY_DOCK_REQUIRED": "it needs the heavy dock", "WEIGHT_EXCEEDS_DOCK_LIMIT": "it is too heavy for the dock",
    "UNLOAD_EXCEEDS_SLOT": "unloading takes longer than the slot", "LABOUR_UNAVAILABLE": "no crew is free then",
    "EQUIPMENT_UNAVAILABLE": "no equipment is available then", "labour_availability": "no labour is recorded for that day",
    "AFTER_LAST_NEW_START": "it starts after the facility's last start time", "OUTSIDE_FACILITY_HOURS": "it is outside opening hours",
    "DOCK_UNAVAILABLE": "the dock is out of service", "DOCK_EVENT_BLOCK": "the dock is blocked then",
    "RELEASE_AFTER_SLOT_START": "the truck cannot arrive by then",
}


def status_of(code: str) -> int:
    return 409 if code in ("SLOT_NOT_AVAILABLE", "ORIGINAL_IN_PROGRESS", "SHIPMENT_FINISHED") else 422


def message_of(exc: AuthError) -> str:
    text = MESSAGES[exc.code]
    if isinstance(exc, Refused):
        return f"{text}: {'; '.join(_WHY.get(r, r) for r in exc.reasons)}."
    return text


def open_slots_page(
    conn: sqlite3.Connection,
    facility_id: str,
    now: datetime,
    *,
    day: date | None = None,
    limit: int | None = None,
) -> tuple[list[sqlite3.Row], bool]:
    """Open, unoccupied, unblocked slots ahead of ``now``. Returns ``(rows, has_more)`` without silent truncation."""
    after = now.astimezone(IST).isoformat()
    where = [
        "s.facility_id=?",
        "s.slot_status='OPEN'",
        "s.slot_start_ts>?",
        f"not exists (select 1 from appointments a where a.slot_id=s.slot_id and a.appointment_status in {_TAKEN})",
    ]
    params: list[object] = [facility_id, after]
    if day is not None:
        day_start = datetime.combine(day, time(0, 0), tzinfo=IST)
        day_end = day_start + timedelta(days=1)
        where.append("s.slot_start_ts>=? and s.slot_start_ts<?")
        params.extend([day_start.isoformat(), day_end.isoformat()])
    sql = (
        "select s.slot_id, d.dock_code, s.slot_start_ts from appointment_slots s join docks d using(dock_id)"
        f" where {' and '.join(where)} order by s.slot_start_ts, d.dock_code"
    )
    free = conn.execute(sql, params).fetchall()
    # A dock event is not written on the slot, so slots under one are dropped here.
    state = build_facility_state(conn, facility_id, now)
    blocked = {i for i, s in state.slots.items() if any(
        e.start < s.end and (e.end is None or e.end > s.start) for e in state.effective_events(s.dock_id))}
    visible = [r for r in free if r["slot_id"] not in blocked]
    if limit is not None and len(visible) > limit:
        return visible[:limit], True
    return visible, False


def open_slots(
    conn: sqlite3.Connection,
    facility_id: str,
    now: datetime,
    *,
    day: date | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Slots of the facility that are ahead of now, open, not booked or held, and not under a dock event."""
    rows, _ = open_slots_page(conn, facility_id, now, day=day, limit=limit)
    return rows


def _arrival(conn: sqlite3.Connection, shipment_id: str, clock: Clock) -> EtaEvidence:
    """When the truck can be there: the load's latest ETA, never before now or before a fresh position allows."""
    # A reported update, otherwise the shipment's own latest figure, otherwise the planned one.
    planned, current = load_eta_records(conn, shipment_id)
    latest = conn.execute("select latest_eta_ts from shipments where shipment_id=?", (shipment_id,)).fetchone()[0]
    record = current or planned
    known = parse_operational_timestamp(latest) if current is None and latest else record.eta if record else clock.now()
    floor = gps_arrival_floor(conn, shipment_id, clock, load_settings())
    eta = max(known, clock.now(), floor or known)
    return EtaEvidence(eta=eta, source="DRIVER_DECLARED", confidence="MEDIUM", baseline_type=BaselineType.CURRENT_ETA)


def assign_in_txn(conn: sqlite3.Connection, *, actor: User, shipment_id: str | None, slot_id: str,
                  clock: Clock) -> tuple[str, datetime]:
    """Book the load into the slot. Call inside a transaction. Returns the dock code and the start (IST)."""
    now = clock.now()
    stamp = now.isoformat()
    ship = conn.execute("select s.driver_id, s.destination_facility_id, s.current_status, f.is_partner from shipments s"
                        " join facilities f on f.facility_id=s.destination_facility_id where s.shipment_id=?",
                        (shipment_id,)).fetchone()
    scope = facility_ids(actor)
    if (shipment_id is None or ship is None or actor.status != "ACTIVE"
            or (scope is not None and ship["destination_facility_id"] not in scope)):
        raise AuthError("SHIPMENT_NOT_FOUND")
    if ship["is_partner"]:
        raise AuthError("PARTNER_FACILITY")
    if ship["current_status"] in ("CANCELLED", "COMPLETED"):
        raise AuthError("SHIPMENT_FINISHED")
    orig = conn.execute("select appointment_id, appointment_status, slot_id from appointments where shipment_id=? and is_current=1"
                        " and appointment_status in ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')", (shipment_id,)).fetchone()
    if release.truck_inside(conn, shipment_id) or (orig is not None and orig["appointment_status"] == "IN_PROGRESS"):
        raise AuthError("ORIGINAL_IN_PROGRESS")
    # An overdue hold the sweep has not cleared yet would still occupy the slot.
    _expire_due_for(conn, now, shipment_id=shipment_id, slot_id=slot_id)
    # The load's own live holds end here: Operations has decided.
    for held in conn.execute("select appointment_id from appointments where shipment_id=? and appointment_status='HELD'",
                             (shipment_id,)).fetchall():
        release_hold_in_txn(conn, held["appointment_id"], shipment_id, "SYSTEM", now, "WAREHOUSE", actor.user_id,
                            "OPERATIONS_ASSIGNED")
    slot = conn.execute("select s.slot_id, s.slot_start_ts, d.dock_code from appointment_slots s join docks d using(dock_id)"
                        " where s.slot_id=? and s.facility_id=? and s.slot_status='OPEN'",
                        (slot_id, ship["destination_facility_id"])).fetchone()
    if slot is None or parse_operational_timestamp(slot["slot_start_ts"]) <= now or conn.execute(
            f"select 1 from appointments where slot_id=? and appointment_status in {_TAKEN}", (slot_id,)).fetchone():
        raise AuthError("SLOT_NOT_AVAILABLE")
    start = parse_operational_timestamp(slot["slot_start_ts"])
    # The same rules the bot books by. A slot that only needs approval is allowed: Operations is the approval.
    state = build_facility_state(conn, ship["destination_facility_id"], now)
    if orig is not None:
        state.occupancy.pop(orig["slot_id"], None)
    eta = _arrival(conn, shipment_id, clock)
    verdict = evaluate_slot(build_context(conn, shipment_id, eta, now), state.slots[slot_id], state)
    if verdict.status in ("INFEASIBLE", "INCOMPLETE_FEASIBILITY_DATA"):
        raise Refused((*verdict.reasons, *verdict.missing))

    if orig is not None:
        conn.execute("update appointments set appointment_status='CANCELLED', is_current=0, cancelled_at=?,"
                     " cancellation_reason='RESCHEDULED', updated_at=? where appointment_id=?",
                     (stamp, stamp, orig["appointment_id"]))
        append_appointment_event(
            conn, orig["appointment_id"], shipment_id, "ORIGINAL_ENDED", from_status=orig["appointment_status"],
            to_status="CANCELLED", actor_type="WAREHOUSE", actor_id=actor.user_id, reason="RESCHEDULED", occurred_at=now)
    appointment_id = "APT-W-" + uuid.uuid4().hex[:10].upper()
    conn.execute("insert into appointments(appointment_id, shipment_id, slot_id, appointment_status, booking_source, is_current,"
                 " booked_at, confirmed_at, replaced_appointment_id, updated_at) values (?,?,?,'CONFIRMED','WAREHOUSE',1,?,?,?,?)",
                 (appointment_id, shipment_id, slot_id, stamp, stamp, orig["appointment_id"] if orig else None, stamp))
    details = {"from_slot_id": orig["slot_id"] if orig else None, "to_slot_id": slot_id, "driver_id": ship["driver_id"]}
    event_id = append_appointment_event(
        conn, appointment_id, shipment_id, "RESCHEDULE_CONFIRMED", from_status=None, to_status="CONFIRMED",
        actor_type="WAREHOUSE", actor_id=actor.user_id, reason="OPERATIONS_ASSIGNED", occurred_at=now, details=details)
    enqueue_downstream(conn, event_id, "RESCHEDULE_CONFIRMED", shipment_id, appointment_id, details, now)
    passes.sync_window(conn, shipment_id, clock)
    audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action="BOOKING_ASSIGNED", target_type="shipment",
                 target_id=shipment_id, outcome="OK", ip=None, detail={"appointment_id": appointment_id, "slot_id": slot_id},
                 clock=clock)
    return str(slot["dock_code"]), start.astimezone(IST)


def cancel_in_txn(conn: sqlite3.Connection, *, actor: User | None, actor_id: str, shipment_id: str | None,
                  clock: Clock) -> tuple[str, datetime] | None:
    """Cancel the shipment's live booking and holds when Operations resolves with CANCELLED_BY_OPERATIONS.

    Call inside a transaction. Returns the freed dock code and slot start (IST), or None if no booking was active.
    """
    if shipment_id is None:
        return None
    ship = conn.execute("select s.current_status, f.is_partner from shipments s join facilities f on f.facility_id=s.destination_facility_id"
                        " where s.shipment_id=?", (shipment_id,)).fetchone()
    if ship is not None and ship["is_partner"]:
        raise AuthError("PARTNER_FACILITY")
    if ship is not None and ship["current_status"] in ("CANCELLED", "COMPLETED"):
        raise AuthError("SHIPMENT_FINISHED")
    now = clock.now()
    stamp = now.isoformat()
    orig = conn.execute(
        "select a.appointment_id, a.appointment_status, a.slot_id, s.slot_start_ts, d.dock_code"
        " from appointments a join appointment_slots s on s.slot_id=a.slot_id join docks d on d.dock_id=s.dock_id"
        " where a.shipment_id=? and a.is_current=1 and a.appointment_status in ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')",
        (shipment_id,),
    ).fetchone()
    if release.truck_inside(conn, shipment_id) or (orig is not None and orig["appointment_status"] == "IN_PROGRESS"):
        raise AuthError("ORIGINAL_IN_PROGRESS")
    for held in conn.execute("select appointment_id from appointments where shipment_id=? and appointment_status='HELD'",
                             (shipment_id,)).fetchall():
        release_hold_in_txn(conn, held["appointment_id"], shipment_id, "SYSTEM", now, "WAREHOUSE", actor_id,
                            "CANCELLED_BY_OPERATIONS")
    if orig is None:
        return None
    conn.execute(
        "update appointments set appointment_status='CANCELLED', is_current=0, cancelled_at=?,"
        " cancellation_reason='CANCELLED_BY_OPERATIONS', updated_at=? where appointment_id=?",
        (stamp, stamp, orig["appointment_id"]),
    )
    event_id = append_appointment_event(
        conn, orig["appointment_id"], shipment_id, "APPOINTMENT_CANCELLED", from_status=orig["appointment_status"],
        to_status="CANCELLED", actor_type="WAREHOUSE", actor_id=actor_id, reason="CANCELLED_BY_OPERATIONS", occurred_at=now,
    )
    enqueue_downstream(conn, event_id, "APPOINTMENT_CANCELLED", shipment_id, orig["appointment_id"],
                       {"cause": "CANCELLED_BY_OPERATIONS"}, now)
    if actor is not None:
        audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action="BOOKING_FREED",
                     target_type="shipment", target_id=shipment_id, outcome="OK", ip=None,
                     detail={"appointment_id": orig["appointment_id"], "cause": "CANCELLED_BY_OPERATIONS"}, clock=clock)
    return str(orig["dock_code"]), parse_operational_timestamp(orig["slot_start_ts"]).astimezone(IST)
