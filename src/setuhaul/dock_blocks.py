"""Blocking a dock for a period and reopening it.

A block is an ordinary dock event, so everything that judges a slot already respects it. `dock_blocks` records who did it,
and ends one block early without changing the event or touching the other blocks on that dock.
"""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from setuhaul import passes
from setuhaul.auth import audit
from setuhaul.auth.authz import facility_ids
from setuhaul.auth.shipments import _facility, _ist
from setuhaul.auth.users import User
from setuhaul.booking import _expire_due_for, release_hold_in_txn
from setuhaul.capacity import BLOCKING_EVENT_TYPES, DockEvent, FacilityState, Occupant, build_facility_state
from setuhaul.clock import Clock
from setuhaul.db import IST, immediate, parse_operational_timestamp
from setuhaul.events import append_appointment_event
from setuhaul.feasibility import build_context, evaluate_slot
from setuhaul.identity import AuthError
from setuhaul.notifications import enqueue_downstream
from setuhaul.release import Booking, booking, free_in_txn, not_freeable, tell_driver
from setuhaul.schemas import BaselineType, EtaEvidence


class BlockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facility_id: str
    dock_id: str
    event_type: Literal["MANUAL_BLOCK", "MAINTENANCE", "BREAKDOWN"]
    starts: datetime
    # Empty: until someone reopens it.
    ends: datetime | None = None
    reason: str = Field(min_length=1, max_length=200)


def _checked(conn: sqlite3.Connection, actor: User, data: BlockRequest, clock: Clock) -> tuple[datetime, datetime | None]:
    """The block's times in India time, if this user may block this dock for them."""
    _facility(conn, actor, data.facility_id)
    if not conn.execute("select 1 from docks where dock_id=? and facility_id=?", (data.dock_id, data.facility_id)).fetchone():
        raise AuthError("DOCK_NOT_FOUND")
    if not data.reason.strip():
        raise AuthError("REASON_REQUIRED")
    starts, ends = _ist(data.starts), None if data.ends is None else _ist(data.ends)
    if ends is not None and (ends <= starts or ends <= clock.now()):
        raise AuthError("INVALID_TIMES")
    return starts, ends


def insert_block_in_txn(conn: sqlite3.Connection, actor: User, data: BlockRequest, clock: Clock) -> str:
    """Store the block and return its event id. Call inside a transaction."""
    starts, ends = _checked(conn, actor, data, clock)
    event_id, stamp = "DEVT-" + uuid.uuid4().hex[:12].upper(), clock.now().astimezone(IST).isoformat()
    conn.execute("insert into dock_status_events(dock_event_id, dock_id, event_type, event_start_ts, event_end_ts, reason,"
                 " created_at) values (?,?,?,?,?,?,?)",
                 (event_id, data.dock_id, data.event_type, starts.isoformat(), None if ends is None else ends.isoformat(),
                  data.reason.strip(), stamp))
    conn.execute("insert into dock_blocks(dock_event_id, created_by, created_at) values (?,?,?)", (event_id, actor.user_id, stamp))
    audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action="DOCK_BLOCKED", target_type="dock",
                 target_id=data.dock_id, outcome="OK", ip=None, detail={"dock_event_id": event_id}, clock=clock)
    return event_id


def current(conn: sqlite3.Connection, facility_ids: Sequence[str], now: datetime) -> list[dict[str, Any]]:
    """Blocks in force or still to come at these facilities, supplied ones included, earliest first."""
    marks = ",".join("?" for _ in facility_ids) or "null"
    rows = conn.execute(
        "select e.dock_event_id, e.dock_id, k.dock_code, k.facility_id, e.event_type, e.event_start_ts as starts,"
        " e.event_end_ts as ends, e.reason, b.ended_at from dock_status_events e join docks k using(dock_id)"
        f" left join dock_blocks b using(dock_event_id) where k.facility_id in ({marks})", tuple(facility_ids)).fetchall()
    out = []
    for r in rows:
        # Reopened here, or already over, or closed by a supplied REOPENED event.
        if r["event_type"] not in BLOCKING_EVENT_TYPES or r["ended_at"] is not None:
            continue
        reopened = conn.execute("select 1 from dock_status_events where dock_id=? and event_type='REOPENED'"
                                " and event_start_ts>=? and event_start_ts<=?",
                                (r["dock_id"], r["starts"], now.astimezone(IST).isoformat())).fetchone()
        if reopened or (r["ends"] is not None and parse_operational_timestamp(r["ends"]) <= now):
            continue
        out.append({k: r[k] for k in ("dock_event_id", "dock_id", "dock_code", "facility_id", "event_type", "starts", "ends",
                                      "reason")})
    return sorted(out, key=lambda b: (parse_operational_timestamp(b["starts"]), b["dock_id"]))


def reopen(conn: sqlite3.Connection, *, actor: User, dock_event_id: str, clock: Clock) -> None:
    """End one block now. A block that had not started never takes effect."""
    with immediate(conn):
        scope = facility_ids(actor)
        mine = [r[0] for r in conn.execute("select facility_id from facilities where is_partner=0")
                if actor.status == "ACTIVE" and (scope is None or r[0] in scope)]
        block = next((b for b in current(conn, mine, clock.now()) if b["dock_event_id"] == dock_event_id), None)
        if block is None:
            raise AuthError("BLOCK_NOT_FOUND")
        now, stamp = clock.now(), clock.now().astimezone(IST).isoformat()
        conn.execute("insert into dock_blocks(dock_event_id, created_at, ended_by, ended_at) values (?,?,?,?)"
                     " on conflict(dock_event_id) do update set ended_by=excluded.ended_by, ended_at=excluded.ended_at",
                     (dock_event_id, stamp, actor.user_id, stamp))
        # Supplied blocks are also written onto the slots themselves; those open again unless another block still covers them.
        state = build_facility_state(conn, block["facility_id"], now)
        starts = parse_operational_timestamp(block["starts"])
        ends = None if block["ends"] is None else parse_operational_timestamp(block["ends"])
        others = state.effective_events(block["dock_id"])
        for slot in state.slots.values():
            if (slot.dock_id != block["dock_id"] or slot.status != "BLOCKED" or slot.end <= now or slot.end <= starts
                    or (ends is not None and slot.start >= ends)
                    or any(e.start < slot.end and (e.end is None or e.end > max(slot.start, now)) for e in others)):
                continue
            conn.execute("update appointment_slots set slot_status='OPEN', block_reason=null where slot_id=?", (slot.slot_id,))
        audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action="DOCK_REOPENED", target_type="dock",
                     target_id=block["dock_id"], outcome="OK", ip=None, detail={"dock_event_id": dock_event_id}, clock=clock)


# ---- warn, move and free --------------------------------------------------------------------------------
# Who is placed first when there are not enough slots.
_PRIORITY = {"CRITICAL": 0, "HIGH": 1, "NORMAL": 2, "LOW": 3}


class PlanItem(BaseModel):
    """One booking caught by a block, and what blocking will do with it."""

    model_config = ConfigDict(frozen=True)

    appointment_id: str
    shipment_id: str
    order_reference: str
    driver_name: str
    dock_code: str
    start: str
    action: Literal["MOVE", "FREE", "SKIP"]
    new_slot_id: str | None = None
    new_dock_code: str | None = None
    new_start: str | None = None
    # For SKIP: why the booking is left where it is.
    why: str | None = None


class Expected(BaseModel):
    """What the warning showed for one booking, sent back to confirm it."""

    model_config = ConfigDict(extra="forbid")

    appointment_id: str
    new_slot_id: str | None = None


class BlockResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    dock_event_id: str
    items: list[PlanItem]
    # Order numbers whose driver the message cannot reach on their phone.
    unreachable: list[str]


class PlanChanged(AuthError):
    """The bookings or free slots are no longer what the warning showed; `items` is the new list."""

    def __init__(self, items: list[PlanItem]) -> None:
        super().__init__("PLAN_CHANGED")
        self.items = items


def _caught(conn: sqlite3.Connection, data: BlockRequest, starts: datetime, ends: datetime | None, now: datetime,
            statuses: tuple[str, ...]) -> list[str]:
    """Appointments on the dock whose slot overlaps the block and has not ended."""
    marks = ",".join("?" for _ in statuses)
    rows = conn.execute(
        "select a.appointment_id, sl.slot_start_ts, sl.slot_end_ts from appointments a join appointment_slots sl using(slot_id)"
        f" where sl.dock_id=? and a.appointment_status in ({marks}) order by sl.slot_start_ts, a.appointment_id",
        (data.dock_id, *statuses))
    out = []
    for r in rows:
        start, end = parse_operational_timestamp(r["slot_start_ts"]), parse_operational_timestamp(r["slot_end_ts"])
        if end > now and end > starts and (ends is None or start < ends):
            out.append(str(r["appointment_id"]))
    return out


def _new_slot(conn: sqlite3.Connection, found: Booking, state: FacilityState, now: datetime) -> str | None:
    """The slot on another dock closest to the original that the booking rules accept, or None."""
    row = conn.execute("select latest_eta_ts, original_eta_ts from shipments where shipment_id=?", (found.shipment_id,)).fetchone()
    raw = row["latest_eta_ts"] or row["original_eta_ts"]
    # Never earlier than now: the truck cannot arrive in the past.
    eta = EtaEvidence(eta=max(parse_operational_timestamp(raw) if raw else found.start, now), source="DRIVER_DECLARED",
                      confidence="HIGH", baseline_type=BaselineType.PLANNED_ETA)
    context = build_context(conn, found.shipment_id, eta, now)
    best = None
    for slot in state.slots.values():
        if slot.dock_id == found.dock_id or slot.start <= now:
            continue
        if evaluate_slot(context, slot, state).status in ("INFEASIBLE", "INCOMPLETE_FEASIBILITY_DATA"):
            continue
        key = (abs(slot.start - found.start), slot.start, state.docks[slot.dock_id].code, slot.slot_id)
        if best is None or key < best:
            best = key
    return None if best is None else best[3]


def _plan(conn: sqlite3.Connection, data: BlockRequest, starts: datetime, ends: datetime | None, now: datetime) -> list[PlanItem]:
    state = build_facility_state(conn, data.facility_id, now)
    # Judge new slots as if the block already existed.
    state.events.append(DockEvent(data.dock_id, data.event_type, starts, ends, data.reason))
    found = [b for b in (booking(conn, a) for a in _caught(conn, data, starts, ends, now,
                                                           ("CONFIRMED", "PENDING_CONFIRMATION", "IN_PROGRESS"))) if b]
    priority = {b.appointment_id: _PRIORITY.get(conn.execute(
        "select priority_code from shipments where shipment_id=?", (b.shipment_id,)).fetchone()[0], 2) for b in found}
    items = []
    for b in sorted(found, key=lambda b: (priority[b.appointment_id], b.start, b.appointment_id)):
        base: dict[str, Any] = {
            "appointment_id": b.appointment_id, "shipment_id": b.shipment_id, "order_reference": b.order_reference,
            "driver_name": b.driver_name, "dock_code": b.dock_code, "start": b.start.isoformat()}
        why = not_freeable(conn, b)
        if why:
            items.append(PlanItem(action="SKIP", why=why, **base))
            continue
        slot_id = _new_slot(conn, b, state, now)
        if slot_id is None:
            items.append(PlanItem(action="FREE", **base))
            continue
        slot = state.slots[slot_id]
        items.append(PlanItem(action="MOVE", new_slot_id=slot_id, new_dock_code=state.docks[slot.dock_id].code,
                              new_start=slot.start.astimezone(IST).isoformat(), **base))
        # Taken from here on, so no slot is proposed twice and crew numbers count the move.
        state.occupancy.pop(b.slot_id, None)
        state.occupancy[slot_id] = Occupant(b.appointment_id, b.shipment_id, "CONFIRMED")
    return items


def preview(conn: sqlite3.Connection, *, actor: User, data: BlockRequest, clock: Clock) -> list[PlanItem]:
    """What blocking would do to the bookings on the dock. Writes nothing."""
    starts, ends = _checked(conn, actor, data, clock)
    return _plan(conn, data, starts, ends, clock.now())


def _move(conn: sqlite3.Connection, actor: User, found: Booking, slot_id: str, clock: Clock) -> None:
    now = clock.now()
    stamp = now.isoformat()
    appointment_id = "APT-W-" + uuid.uuid4().hex[:10].upper()
    # An overdue hold the sweep has not cleared yet would still occupy the slot.
    _expire_due_for(conn, now, shipment_id=found.shipment_id, slot_id=slot_id)
    conn.execute("insert into appointments(appointment_id, shipment_id, slot_id, appointment_status, booking_source, is_current,"
                 " booked_at, confirmed_at, replaced_appointment_id, updated_at) values (?,?,?,'CONFIRMED','WAREHOUSE',1,?,?,?,?)",
                 (appointment_id, found.shipment_id, slot_id, stamp, stamp, found.appointment_id, stamp))
    details = {"from_slot_id": found.slot_id, "to_slot_id": slot_id, "driver_id": found.driver_id}
    event_id = append_appointment_event(
        conn, appointment_id, found.shipment_id, "RESCHEDULE_CONFIRMED", from_status=None, to_status="CONFIRMED",
        actor_type="WAREHOUSE", actor_id=actor.user_id, reason="DOCK_BLOCKED", occurred_at=now, details=details)
    enqueue_downstream(conn, event_id, "RESCHEDULE_CONFIRMED", found.shipment_id, appointment_id, details, now)
    passes.sync_window(conn, found.shipment_id, clock)


def block(conn: sqlite3.Connection, *, actor: User, data: BlockRequest, expected: list[Expected], clock: Clock,
          telegram_on: bool) -> BlockResult:
    """Block the dock and do what the warning showed, all of it or nothing; then tell the drivers."""
    with immediate(conn):
        now = clock.now()
        starts, ends = _checked(conn, actor, data, clock)
        items = _plan(conn, data, starts, ends, now)
        acting = [i for i in items if i.action != "SKIP"]
        if sorted((i.appointment_id, i.new_slot_id or "") for i in acting) != sorted(
                (e.appointment_id, e.new_slot_id or "") for e in expected):
            raise PlanChanged(items)
        # Holds of shipments with no booking being changed here: they are released and their drivers told.
        moving = {i.shipment_id for i in acting}
        held = []
        for appointment_id in _caught(conn, data, starts, ends, now, ("HELD",)):
            found = booking(conn, appointment_id)
            if found is not None and found.shipment_id not in moving:
                held.append(found)
        event_id = insert_block_in_txn(conn, actor, data, clock)
        for h in held:
            release_hold_in_txn(conn, h.appointment_id, h.shipment_id, "SYSTEM", now, "WAREHOUSE", actor.user_id, "DOCK_BLOCKED")
        done = []
        for item in acting:
            found = booking(conn, item.appointment_id)
            assert found is not None
            free_in_txn(conn, found, "DOCK_BLOCKED", actor, clock, announce=item.action == "FREE")
            done.append((item, found))
        for item, found in done:
            if item.new_slot_id:
                _move(conn, actor, found, item.new_slot_id, clock)
    unreachable = []
    for item, found in done:
        fields = {"order": found.order_reference, "dock": found.dock_code, "start": f"{found.start:%H:%M}",
                  "date": f"{found.start:%d %b %Y}"}
        key = "slot_freed"
        if item.new_start:
            new_start = datetime.fromisoformat(item.new_start)
            key, fields = "dock_moved", {"order": found.order_reference, "old_dock": found.dock_code,
                                         "dock": str(item.new_dock_code), "start": f"{new_start:%H:%M}",
                                         "date": f"{new_start:%d %b %Y}"}
        if not tell_driver(conn, clock, telegram_on, found, key, **fields):
            unreachable.append(found.order_reference)
    for h in held:
        if not tell_driver(conn, clock, telegram_on, h, "hold_blocked", order=h.order_reference, dock=h.dock_code):
            unreachable.append(h.order_reference)
    return BlockResult(dock_event_id=event_id, items=items, unreachable=unreachable)
