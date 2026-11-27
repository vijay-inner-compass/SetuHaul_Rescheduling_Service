"""Read-only capacity state: slots, docks, blocks, occupancy, labour and consignee inputs.

``build_facility_state`` loads one facility into memory so slot evaluation is pure and fast.
"""

# Lazy annotations.
from __future__ import annotations

# hashlib/json build the version fingerprints.
import hashlib
import json

# sqlite3 for the Connection type.
import sqlite3

# dataclass/field for the in-memory state objects.
from dataclasses import dataclass, field

# datetime for instants; time for the facility's opening hours (a clock time without a date).
from datetime import datetime, time, timedelta

# ZoneInfo converts opening hours into real instants in the facility's timezone.
from zoneinfo import ZoneInfo

# Timestamp parsing.
from setuhaul.db import parse_operational_timestamp

# The consignee commitment contract.
from setuhaul.schemas import ConsigneeCommitment

# Statuses that consume capacity. HELD is added to the appointment CHECK in Phase 5; including it
# here means holds count the moment they exist.
ACTIVE_STATUSES = ("PENDING_CONFIRMATION", "CONFIRMED", "IN_PROGRESS", "HELD")
# Dock events that make a dock unusable for their duration.
BLOCKING_EVENT_TYPES = ("MAINTENANCE", "BREAKDOWN", "MANUAL_BLOCK", "CAPACITY_REDUCTION")


# One bookable time window on one dock.
@dataclass(frozen=True)
class SlotRow:
    slot_id: str
    facility_id: str
    dock_id: str
    start: datetime
    end: datetime
    # OPEN, or a blocked state set by planners.
    status: str
    block_reason: str | None


# A physical dock and what it can handle.
@dataclass(frozen=True)
class DockRow:
    dock_id: str
    # The short name drivers see (D1, D2 ...).
    code: str
    dock_type: str
    supports_refrigerated: bool
    max_weight_kg: int
    status: str


# A maintenance, breakdown, reopening or similar event on a dock.
@dataclass(frozen=True)
class DockEvent:
    dock_id: str
    event_type: str
    start: datetime
    # None means open-ended (no end recorded yet).
    end: datetime | None
    reason: str


# Who currently occupies a slot.
@dataclass(frozen=True)
class Occupant:
    appointment_id: str
    shipment_id: str
    status: str


# A period with a stated number of labour crews and equipment availability.
@dataclass(frozen=True)
class LabourWindow:
    start: datetime
    end: datetime
    crews: int
    equipment_available: bool


# Everything about one facility, loaded once so a whole slot search touches the database only a handful of times.
@dataclass
class FacilityState:
    facility_id: str
    # The moment this snapshot was taken; feasibility refuses a snapshot that is too old.
    as_of: datetime
    timezone: str
    open_time: time
    close_time: time
    # Active facility rules by name (for example LAST_NEW_START_TIME).
    rules: dict[str, str]
    slots: dict[str, SlotRow]
    docks: dict[str, DockRow]
    events: list[DockEvent]
    # slot_id -> who occupies it (only live appointments and unexpired holds).
    occupancy: dict[str, Occupant]
    labour: list[LabourWindow] = field(default_factory=list)

    def rule_int(self, name: str) -> int | None:
        # A numeric rule value, or None if the rule is not set for this facility.
        raw = self.rules.get(name)
        return int(raw) if raw is not None else None

    def effective_events(self, dock_id: str) -> list[DockEvent]:
        """Blocking events for a dock, with open-ended ones closed by a later REOPENED event."""
        # Times at which the dock was declared reopened, in order.
        reopen = sorted(e.start for e in self.events if e.dock_id == dock_id and e.event_type == "REOPENED")
        out: list[DockEvent] = []
        for e in self.events:
            # Only this dock's blocking events count.
            if e.dock_id != dock_id or e.event_type not in BLOCKING_EVENT_TYPES:
                continue
            end = e.end
            # The first reopening at or after the block starts.
            closing = next((r for r in reopen if r >= e.start), None)
            # If it comes before the block's own end (or the block has no end), the block really ends there.
            if closing is not None and (end is None or closing < end):
                end = closing
            out.append(DockEvent(e.dock_id, e.event_type, e.start, end, e.reason))
        return out

    def slot_version(self, slot_id: str) -> str:
        """Fingerprint of every mutable fact that decides whether this slot can be offered."""
        slot = self.slots.get(slot_id)
        if slot is None:
            return "MISSING"
        dock = self.docks.get(slot.dock_id)
        occ = self.occupancy.get(slot_id)
        # Collect every fact that could change whether the slot is offerable: its own state, its dock,
        # any dock events overlapping it, and who occupies it.
        payload = {
            "slot": [slot.status, slot.block_reason, slot.start.isoformat(), slot.end.isoformat()],
            "dock": [dock.status, dock.dock_type, dock.max_weight_kg] if dock else None,
            "events": [
                [e.event_type, e.start.isoformat(), e.end.isoformat() if e.end else None]
                for e in self.effective_events(slot.dock_id)
                if e.start < slot.end and (e.end is None or e.end > slot.start)
            ],
            "occupant": [occ.appointment_id, occ.status] if occ else None,
        }
        # A short hash of those facts. If any fact changes between "options shown" and "hold requested", the hash differs,
        # which is how the system notices that a shown option has gone stale.
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]

    def rules_version(self) -> str:
        # A fingerprint of the facility rules, so a rule change is detected the same way.
        blob = json.dumps(sorted(self.rules.items()))
        return "R1-" + hashlib.sha256(blob.encode()).hexdigest()[:10]

    def active_overlapping(self, slot: SlotRow, exclude_shipment: str | None = None) -> int:
        """Active appointments at this facility whose slot overlaps ``slot``."""
        n = 0
        for slot_id, occ in self.occupancy.items():
            other = self.slots.get(slot_id)
            # Skip unknown slots and the requesting shipment's own booking (it will move, so it does not compete).
            if other is None or (exclude_shipment and occ.shipment_id == exclude_shipment):
                continue
            # The interval-overlap test: each starts before the other ends.
            if other.start < slot.end and slot.start < other.end:
                n += 1
        return n


def _hhmm(raw: str) -> time:
    # "08:00" -> time(8, 0).
    h, m = raw.split(":")
    return time(int(h), int(m))


def build_facility_state(conn: sqlite3.Connection, facility_id: str, as_of: datetime) -> FacilityState:
    # The facility's own settings: timezone and opening hours.
    fac = conn.execute(
        "select timezone, open_time, close_time from facilities where facility_id=?", (facility_id,)
    ).fetchone()
    # An unknown facility is a caller error.
    if fac is None:
        raise KeyError(facility_id)
    # Active rules by name (only rows with active_flag=1).
    rules = {
        r["rule_type"]: r["rule_value"]
        for r in conn.execute(
            "select rule_type, rule_value from facility_rules where facility_id=? and active_flag=1",
            (facility_id,),
        )
    }
    # Docks, keyed by id.
    docks = {
        r["dock_id"]: DockRow(
            r["dock_id"], r["dock_code"], r["dock_type"], bool(r["supports_refrigerated"]),
            r["max_vehicle_weight_kg"], r["dock_status"],
        )
        for r in conn.execute("select * from docks where facility_id=?", (facility_id,))
    }
    # Slots, keyed by id, with timestamps parsed into aware datetimes.
    slots = {
        r["slot_id"]: SlotRow(
            r["slot_id"], r["facility_id"], r["dock_id"],
            parse_operational_timestamp(r["slot_start_ts"]), parse_operational_timestamp(r["slot_end_ts"]),
            r["slot_status"], r["block_reason"],
        )
        for r in conn.execute("select * from appointment_slots where facility_id=?", (facility_id,))
    }
    # Dock events for this facility's docks; an empty end time means open-ended.
    events = []
    for r in conn.execute(
        "select e.*, b.ended_at from dock_status_events e join docks d using(dock_id) "
        "left join dock_blocks b using(dock_event_id) where d.facility_id=?",
        (facility_id,),
    ):
        start = parse_operational_timestamp(r["event_start_ts"])
        end = parse_operational_timestamp(r["event_end_ts"]) if r["event_end_ts"] else None
        # A block reopened from the screen ends then; reopened before it began, it never applies.
        if r["ended_at"]:
            ended = parse_operational_timestamp(r["ended_at"])
            end = ended if end is None else min(end, ended)
            if end <= start:
                continue
        events.append(DockEvent(r["dock_id"], r["event_type"], start, end, r["reason"]))
    # One "?" per active status, for the SQL IN (...) list.
    marks = ",".join("?" for _ in ACTIVE_STATUSES)
    # A hold past its expiry no longer consumes capacity, whether or not the sweep has run yet.
    # This is what makes a lapsed hold's slot offerable again the instant its time is up, not only after the sweep job runs.
    occupancy = {
        r["slot_id"]: Occupant(r["appointment_id"], r["shipment_id"], r["appointment_status"])
        for r in conn.execute(
            f"select a.appointment_id, a.shipment_id, a.slot_id, a.appointment_status from appointments a "
            f"join appointment_slots s using(slot_id) "
            f"left join appointment_hold_meta m on m.appointment_id = a.appointment_id "
            f"where s.facility_id=? and a.appointment_status in ({marks}) "
            f"and not (a.appointment_status='HELD' and m.expires_epoch is not null and m.expires_epoch <= ?)",
            (facility_id, *ACTIVE_STATUSES, int(as_of.timestamp())),
        )
    }
    # Labour crew and equipment windows.
    labour = [
        LabourWindow(
            parse_operational_timestamp(r["window_start"]), parse_operational_timestamp(r["window_end"]),
            r["available_crews"], bool(r["equipment_available"]),
        )
        for r in conn.execute("select * from labour_windows where facility_id=?", (facility_id,))
    ]
    # Assemble the snapshot.
    return FacilityState(
        facility_id=facility_id, as_of=as_of, timezone=fac["timezone"],
        open_time=_hhmm(fac["open_time"]), close_time=_hhmm(fac["close_time"]),
        rules=rules, slots=slots, docks=docks, events=events, occupancy=occupancy, labour=labour,
    )


def load_consignee(conn: sqlite3.Connection, shipment_id: str) -> ConsigneeCommitment | None:
    """None means the commitment status is UNKNOWN (no row). A row with no deadline is known."""
    r = conn.execute("select * from consignee_commitments where shipment_id=?", (shipment_id,)).fetchone()
    # No row: we do not know, and feasibility will refuse to assume.
    if r is None:
        return None
    # A row exists: parse the deadline (if any) and the penalty flags.
    return ConsigneeCommitment(
        latest_arrival=parse_operational_timestamp(r["latest_arrival"]) if r["latest_arrival"] else None,
        penalty_applies=bool(r["penalty_applies"]),
        exceptional=bool(r["exceptional"]),
    )


def local_bounds(state: FacilityState, slot: SlotRow) -> tuple[datetime, datetime]:
    """Facility opening and closing instants on the slot's local date."""
    tz = ZoneInfo(state.timezone)
    # The slot's calendar date in the facility's own timezone.
    day = slot.start.astimezone(tz).date()
    # Combine that date with the opening and closing clock times to get exact instants.
    # Closing at or before opening means the next day: 00:00 to 00:00 is open all day.
    closing_day = day + timedelta(days=1) if state.close_time <= state.open_time else day
    return (
        datetime.combine(day, state.open_time, tzinfo=tz),
        datetime.combine(closing_day, state.close_time, tzinfo=tz),
    )
