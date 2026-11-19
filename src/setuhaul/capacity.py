"""Read-only capacity state: slots, docks, blocks, occupancy, labour and consignee inputs.

``build_facility_state`` loads one facility into memory so slot evaluation is pure and fast.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

from setuhaul.db import parse_operational_timestamp
from setuhaul.schemas import ConsigneeCommitment

# Statuses that consume capacity. HELD is added to the appointment CHECK in Phase 5; including it
# here means holds count the moment they exist.
ACTIVE_STATUSES = ("PENDING_CONFIRMATION", "CONFIRMED", "IN_PROGRESS", "HELD")
BLOCKING_EVENT_TYPES = ("MAINTENANCE", "BREAKDOWN", "MANUAL_BLOCK", "CAPACITY_REDUCTION")


@dataclass(frozen=True)
class SlotRow:
    slot_id: str
    facility_id: str
    dock_id: str
    start: datetime
    end: datetime
    status: str
    block_reason: str | None


@dataclass(frozen=True)
class DockRow:
    dock_id: str
    code: str
    dock_type: str
    supports_refrigerated: bool
    max_weight_kg: int
    status: str


@dataclass(frozen=True)
class DockEvent:
    dock_id: str
    event_type: str
    start: datetime
    end: datetime | None
    reason: str


@dataclass(frozen=True)
class Occupant:
    appointment_id: str
    shipment_id: str
    status: str


@dataclass(frozen=True)
class LabourWindow:
    start: datetime
    end: datetime
    crews: int
    equipment_available: bool


@dataclass
class FacilityState:
    facility_id: str
    as_of: datetime
    timezone: str
    open_time: time
    close_time: time
    rules: dict[str, str]
    slots: dict[str, SlotRow]
    docks: dict[str, DockRow]
    events: list[DockEvent]
    occupancy: dict[str, Occupant]
    labour: list[LabourWindow] = field(default_factory=list)

    def rule_int(self, name: str) -> int | None:
        raw = self.rules.get(name)
        return int(raw) if raw is not None else None

    def effective_events(self, dock_id: str) -> list[DockEvent]:
        """Blocking events for a dock, with open-ended ones closed by a later REOPENED event."""
        reopen = sorted(e.start for e in self.events if e.dock_id == dock_id and e.event_type == "REOPENED")
        out: list[DockEvent] = []
        for e in self.events:
            if e.dock_id != dock_id or e.event_type not in BLOCKING_EVENT_TYPES:
                continue
            end = e.end
            closing = next((r for r in reopen if r >= e.start), None)
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
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]

    def rules_version(self) -> str:
        blob = json.dumps(sorted(self.rules.items()))
        return "R1-" + hashlib.sha256(blob.encode()).hexdigest()[:10]

    def active_overlapping(self, slot: SlotRow, exclude_shipment: str | None = None) -> int:
        """Active appointments at this facility whose slot overlaps ``slot``."""
        n = 0
        for slot_id, occ in self.occupancy.items():
            other = self.slots.get(slot_id)
            if other is None or (exclude_shipment and occ.shipment_id == exclude_shipment):
                continue
            if other.start < slot.end and slot.start < other.end:
                n += 1
        return n


def _hhmm(raw: str) -> time:
    h, m = raw.split(":")
    return time(int(h), int(m))


def build_facility_state(conn: sqlite3.Connection, facility_id: str, as_of: datetime) -> FacilityState:
    fac = conn.execute(
        "select timezone, open_time, close_time from facilities where facility_id=?", (facility_id,)
    ).fetchone()
    if fac is None:
        raise KeyError(facility_id)
    rules = {
        r["rule_type"]: r["rule_value"]
        for r in conn.execute(
            "select rule_type, rule_value from facility_rules where facility_id=? and active_flag=1",
            (facility_id,),
        )
    }
    docks = {
        r["dock_id"]: DockRow(
            r["dock_id"], r["dock_code"], r["dock_type"], bool(r["supports_refrigerated"]),
            r["max_vehicle_weight_kg"], r["dock_status"],
        )
        for r in conn.execute("select * from docks where facility_id=?", (facility_id,))
    }
    slots = {
        r["slot_id"]: SlotRow(
            r["slot_id"], r["facility_id"], r["dock_id"],
            parse_operational_timestamp(r["slot_start_ts"]), parse_operational_timestamp(r["slot_end_ts"]),
            r["slot_status"], r["block_reason"],
        )
        for r in conn.execute("select * from appointment_slots where facility_id=?", (facility_id,))
    }
    events = [
        DockEvent(
            r["dock_id"], r["event_type"], parse_operational_timestamp(r["event_start_ts"]),
            parse_operational_timestamp(r["event_end_ts"]) if r["event_end_ts"] else None, r["reason"],
        )
        for r in conn.execute(
            "select e.* from dock_status_events e join docks d using(dock_id) where d.facility_id=?",
            (facility_id,),
        )
    ]
    marks = ",".join("?" for _ in ACTIVE_STATUSES)
    occupancy = {
        r["slot_id"]: Occupant(r["appointment_id"], r["shipment_id"], r["appointment_status"])
        for r in conn.execute(
            f"select a.appointment_id, a.shipment_id, a.slot_id, a.appointment_status from appointments a "
            f"join appointment_slots s using(slot_id) where s.facility_id=? "
            f"and a.appointment_status in ({marks})",
            (facility_id, *ACTIVE_STATUSES),
        )
    }
    labour = [
        LabourWindow(
            parse_operational_timestamp(r["window_start"]), parse_operational_timestamp(r["window_end"]),
            r["available_crews"], bool(r["equipment_available"]),
        )
        for r in conn.execute("select * from labour_windows where facility_id=?", (facility_id,))
    ]
    return FacilityState(
        facility_id=facility_id, as_of=as_of, timezone=fac["timezone"],
        open_time=_hhmm(fac["open_time"]), close_time=_hhmm(fac["close_time"]),
        rules=rules, slots=slots, docks=docks, events=events, occupancy=occupancy, labour=labour,
    )


def load_consignee(conn: sqlite3.Connection, shipment_id: str) -> ConsigneeCommitment | None:
    """None means the commitment status is UNKNOWN (no row). A row with no deadline is known."""
    r = conn.execute("select * from consignee_commitments where shipment_id=?", (shipment_id,)).fetchone()
    if r is None:
        return None
    return ConsigneeCommitment(
        latest_arrival=parse_operational_timestamp(r["latest_arrival"]) if r["latest_arrival"] else None,
        penalty_applies=bool(r["penalty_applies"]),
        exceptional=bool(r["exceptional"]),
    )


def local_bounds(state: FacilityState, slot: SlotRow) -> tuple[datetime, datetime]:
    """Facility opening and closing instants on the slot's local date."""
    tz = ZoneInfo(state.timezone)
    day = slot.start.astimezone(tz).date()
    return (
        datetime.combine(day, state.open_time, tzinfo=tz),
        datetime.combine(day, state.close_time, tzinfo=tz),
    )
