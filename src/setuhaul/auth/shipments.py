"""Warehouse operations behind the Shipments tab: open availability, then create and manage assigned shipments.

Everything written here is marked as entered in the app, so it is never mistaken for supplied data. Nothing is assumed:
availability and the receiver's commitment are only what the user typed.
"""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime, time, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from setuhaul.auth import audit
from setuhaul.auth.authz import facility_ids
from setuhaul.auth.onboarding import driver_eligible
from setuhaul.auth.users import User
from setuhaul.auth.warehouse_routes import drivers_in_scope
from setuhaul.capacity import build_facility_state
from setuhaul.clock import Clock
from setuhaul.db import IST, immediate, parse_operational_timestamp
from setuhaul.feasibility import build_context, evaluate_slot
from setuhaul.identity import AuthError
from setuhaul.order_numbers import is_valid, normalise
from setuhaul.schemas import BaselineType, EtaEvidence

# Rows typed into the app carry this source.
SOURCE = "WAREHOUSE_UI"
# Availability is opened at most this many days at a time.
MAX_DAYS = 14
# Appointments that keep a slot busy.
_TAKEN = "('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS','HELD')"


class Availability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facility_id: str
    first_date: date
    last_date: date
    dock_ids: list[str]
    from_time: time
    to_time: time
    slot_minutes: int = Field(60, ge=15, le=480)
    crews: int = Field(ge=1, le=100)
    equipment_available: bool = True


class AvailabilityResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    slots_created: int
    # Days that already had labour recorded, which is left as it was.
    days_unchanged: list[str]


class NewShipment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_number: str = Field(max_length=40)
    driver_id: str
    vehicle_id: str
    origin_name: str = Field(min_length=1, max_length=120)
    origin_city: str = Field(min_length=1, max_length=80)
    facility_id: str
    customer_name: str = Field(min_length=1, max_length=120)
    product_category: str = Field(min_length=1, max_length=80)
    load_weight_kg: int = Field(gt=0, le=100_000)
    pallet_count: int | None = Field(None, ge=0, le=1000)
    required_dock_type: Literal["ANY", "STANDARD", "REEFER", "HEAVY"] = "ANY"
    temperature_control_required: bool = False
    priority_code: Literal["LOW", "NORMAL", "HIGH", "CRITICAL"] = "NORMAL"
    expected_unload_min: int = Field(gt=0, le=720)
    planned_departure: datetime
    slot_id: str
    latest_arrival: datetime | None = None
    penalty_applies: bool = False


def _ist(value: datetime) -> datetime:
    # A value from a form has no offset: it is operational (IST) time.
    return value.replace(tzinfo=IST) if value.tzinfo is None else value.astimezone(IST)


def _facility(conn: sqlite3.Connection, actor: User, facility_id: str) -> sqlite3.Row:
    # Out of the user's scope looks exactly like a facility that does not exist.
    scope = facility_ids(actor)
    row = conn.execute("select * from facilities where facility_id=? and active_flag=1", (facility_id,)).fetchone()
    if actor.status != "ACTIVE" or row is None or (scope is not None and facility_id not in scope):
        raise AuthError("FACILITY_NOT_FOUND")
    if row["is_partner"]:
        raise AuthError("PARTNER_FACILITY")
    assert isinstance(row, sqlite3.Row)
    return row


def open_availability(conn: sqlite3.Connection, *, actor: User, data: Availability, clock: Clock) -> AvailabilityResult:
    """Create the missing dock slots and one labour window per day. Existing rows are never changed."""
    with immediate(conn):
        facility = _facility(conn, actor, data.facility_id)
        now = clock.now()
        days = (data.last_date - data.first_date).days + 1
        if data.first_date < now.astimezone(IST).date() or not 1 <= days <= MAX_DAYS:
            raise AuthError("INVALID_DATES")
        opens, closes = time.fromisoformat(facility["open_time"]), time.fromisoformat(facility["close_time"])
        if not opens <= data.from_time < data.to_time <= closes:
            raise AuthError("INVALID_TIMES")
        docks = sorted(set(data.dock_ids))
        own = {r["dock_id"] for r in conn.execute("select dock_id from docks where facility_id=?", (data.facility_id,))}
        if not docks or not set(docks) <= own:
            raise AuthError("DOCK_NOT_FOUND")
        step, stamp, created, unchanged = timedelta(minutes=data.slot_minutes), now.isoformat(), 0, []
        for offset in range(days):
            day = data.first_date + timedelta(days=offset)
            start = datetime.combine(day, data.from_time, tzinfo=IST)
            end = datetime.combine(day, data.to_time, tzinfo=IST)
            if conn.execute("select 1 from labour_windows where facility_id=? and substr(window_start,1,10)=?",
                            (data.facility_id, day.isoformat())).fetchone():
                unchanged.append(day.isoformat())
            else:
                conn.execute("insert into labour_windows(facility_id, window_start, window_end, available_crews, equipment_available,"
                             " source, recorded_at) values (?,?,?,?,?,?,?)",
                             (data.facility_id, start.isoformat(), end.isoformat(), data.crews, int(data.equipment_available),
                              SOURCE, stamp))
            for dock in docks:
                at = start
                while at + step <= end:
                    # Any slot overlapping this time on this dock means it is already described.
                    if not conn.execute("select 1 from appointment_slots where dock_id=? and slot_start_ts<? and slot_end_ts>?",
                                        (dock, (at + step).isoformat(), at.isoformat())).fetchone():
                        conn.execute("insert into appointment_slots(slot_id, facility_id, dock_id, slot_start_ts, slot_end_ts,"
                                     " slot_status, created_at) values (?,?,?,?,?,'OPEN',?)",
                                     ("SL-" + uuid.uuid4().hex[:10].upper(), data.facility_id, dock, at.isoformat(),
                                      (at + step).isoformat(), stamp))
                        created += 1
                    at += step
        audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action="AVAILABILITY_OPENED",
                     target_type="facility", target_id=data.facility_id, outcome="OK", ip=None,
                     detail={"first": data.first_date.isoformat(), "last": data.last_date.isoformat(), "slots": created},
                     clock=clock)
    return AvailabilityResult(slots_created=created, days_unchanged=unchanged)


def _driver(conn: sqlite3.Connection, actor: User, driver_id: str, vehicle_id: str, today: date) -> str:
    """The carrier of a driver the user may see and assign, driving one of that carrier's active vehicles."""
    found = drivers_in_scope(conn, actor, today, driver_id)
    if not found:
        raise AuthError("DRIVER_NOT_FOUND")
    if not driver_eligible(conn, driver_id, today):
        raise AuthError("DRIVER_NOT_ELIGIBLE")
    carrier = str(found[0]["carrier_id"])
    if not conn.execute("select 1 from vehicles where vehicle_id=? and carrier_id=? and active_flag=1", (vehicle_id, carrier)).fetchone():
        raise AuthError("VEHICLE_CARRIER_MISMATCH")
    return carrier


def create_shipment(conn: sqlite3.Connection, *, actor: User, data: NewShipment, clock: Clock) -> str:
    """The shipment, its planned appointment, the receiver's commitment and the pending assignment, or nothing at all."""
    if not is_valid(data.order_number):
        raise AuthError("BAD_ORDER_NUMBER")
    order = data.order_number.strip().upper()
    with immediate(conn):
        now = clock.now()
        _facility(conn, actor, data.facility_id)
        if any(normalise(r[0]) == normalise(order) for r in conn.execute("select order_reference from shipments")):
            raise AuthError("DUPLICATE_ORDER_NUMBER")
        carrier = _driver(conn, actor, data.driver_id, data.vehicle_id, now.astimezone(IST).date())
        slot = conn.execute("select * from appointment_slots where slot_id=? and facility_id=? and slot_status='OPEN'",
                            (data.slot_id, data.facility_id)).fetchone()
        if slot is None or parse_operational_timestamp(slot["slot_start_ts"]) <= now or conn.execute(
                f"select 1 from appointments where slot_id=? and appointment_status in {_TAKEN}", (data.slot_id,)).fetchone():
            raise AuthError("SLOT_NOT_AVAILABLE")
        start = parse_operational_timestamp(slot["slot_start_ts"])
        departure = _ist(data.planned_departure)
        if departure >= start:
            raise AuthError("INVALID_DEPARTURE")
        shipment_id, stamp = "SHP-" + uuid.uuid4().hex[:8].upper(), now.isoformat()
        conn.execute(
            "insert into shipments(shipment_id, order_reference, carrier_id, driver_id, vehicle_id, origin_name, origin_city,"
            " destination_facility_id, customer_name, product_category, load_weight_kg, pallet_count, required_dock_type,"
            " temperature_control_required, priority_code, planned_departure_ts, original_eta_ts, latest_eta_ts,"
            " expected_unload_min, current_status, created_at, updated_at) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'ASSIGNED',?,?)",
            (shipment_id, order, carrier, data.driver_id, data.vehicle_id, data.origin_name.strip(), data.origin_city.strip(),
             data.facility_id, data.customer_name.strip(), data.product_category.strip(), data.load_weight_kg, data.pallet_count,
             data.required_dock_type, int(data.temperature_control_required), data.priority_code, departure.isoformat(),
             start.isoformat(), start.isoformat(), data.expected_unload_min, stamp, stamp))
        conn.execute("insert into consignee_commitments(shipment_id, latest_arrival, penalty_applies, exceptional, source, recorded_at)"
                     " values (?,?,?,0,?,?)",
                     (shipment_id, _ist(data.latest_arrival).isoformat() if data.latest_arrival else None,
                      int(data.penalty_applies), SOURCE, stamp))
        conn.execute("insert into shipment_assignments(shipment_id, created_by, created_at) values (?,?,?)",
                     (shipment_id, actor.user_id, stamp))
        # The same rules the bot books by must accept the planned slot; a refusal rolls all of the above back.
        eta = EtaEvidence(eta=start, source="DRIVER_DECLARED", confidence="HIGH", baseline_type=BaselineType.PLANNED_ETA)
        state = build_facility_state(conn, data.facility_id, now)
        verdict = evaluate_slot(build_context(conn, shipment_id, eta, now), state.slots[data.slot_id], state)
        if verdict.status in ("INFEASIBLE", "INCOMPLETE_FEASIBILITY_DATA"):
            raise AuthError("LOAD_DOES_NOT_FIT")
        # Booked only after the check: a shipment's own current slot is never judged feasible.
        conn.execute("insert into appointments(appointment_id, shipment_id, slot_id, appointment_status, booking_source, is_current,"
                     " booked_at, confirmed_at, updated_at) values (?,?,?,'CONFIRMED','WAREHOUSE',1,?,?,?)",
                     ("APT-W-" + uuid.uuid4().hex[:10].upper(), shipment_id, data.slot_id, stamp, stamp, stamp))
        audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action="SHIPMENT_CREATED", target_type="shipment",
                     target_id=shipment_id, outcome="OK", ip=None,
                     detail={"driver_id": data.driver_id, "facility_id": data.facility_id}, clock=clock)
    return shipment_id
