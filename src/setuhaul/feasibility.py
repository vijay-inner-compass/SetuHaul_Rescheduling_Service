"""Slot feasibility: pure evaluation over an in-memory FacilityState.

Hard facts decide INFEASIBLE. Missing evidence (labour, consignee, stale facility state) decides
INCOMPLETE_FEASIBILITY_DATA and is never replaced by an assumption. A penalty or exceptional
consignee commitment decides REQUIRES_APPROVAL rather than a silent choice.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

from setuhaul.capacity import (
    FacilityState,
    SlotRow,
    build_facility_state,
    load_consignee,
    local_bounds,
)
from setuhaul.clock import Clock
from setuhaul.db import parse_operational_timestamp
from setuhaul.schemas import (
    Aware,
    ClarificationNeeded,
    ConsigneeCommitment,
    DriverConstraints,
    EtaConflict,
    EtaEvidence,
    UnloadRequirement,
)

Status = Literal["FEASIBLE", "INFEASIBLE", "REQUIRES_APPROVAL", "INCOMPLETE_FEASIBILITY_DATA"]
DEFAULT_MAX_STATE_AGE = timedelta(minutes=5)


class FeasibilityContext(BaseModel):
    """Everything about the requesting shipment that decides slot eligibility."""

    model_config = ConfigDict(frozen=True)

    shipment_id: str
    driver_id: str
    facility_id: str
    unload: UnloadRequirement
    release_time: Aware
    release_source: str
    constraints: DriverConstraints = DriverConstraints()
    consignee: ConsigneeCommitment | None = None
    own_slot_id: str | None = None
    now: Aware
    eta_buffer_minutes: int = 0
    max_state_age_seconds: int = int(DEFAULT_MAX_STATE_AGE.total_seconds())

    def with_now(self, now: datetime) -> FeasibilityContext:
        return self.model_copy(update={"now": now})


class FeasibilityDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    slot_id: str
    status: Status
    reasons: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    wait_minutes: int | None = None
    slot_start: Aware
    slot_end: Aware
    dock_id: str
    dock_code: str
    dock_type: str
    slot_version: str

    @property
    def digest(self) -> str:
        blob = self.model_dump_json(exclude={"slot_start", "slot_end"})
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


class SearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal[
        "OK", "NO_SLOT", "REQUIRES_APPROVAL", "INCOMPLETE_FEASIBILITY_DATA", "NEEDS_INPUT", "ETA_CONFLICT"
    ]
    feasible: tuple[FeasibilityDecision, ...] = ()
    requires_approval: tuple[FeasibilityDecision, ...] = ()
    incomplete: tuple[FeasibilityDecision, ...] = ()
    rejection_counts: dict[str, int] = {}
    detail: str | None = None


def _decision(slot: SlotRow, state: FacilityState, ctx: FeasibilityContext, status: Status, **kw: object) -> FeasibilityDecision:
    dock = state.docks[slot.dock_id]
    release = ctx.release_time + timedelta(minutes=ctx.eta_buffer_minutes)
    wait = int((slot.start - release).total_seconds() // 60)
    return FeasibilityDecision(
        slot_id=slot.slot_id, status=status, wait_minutes=wait, slot_start=slot.start, slot_end=slot.end,
        dock_id=dock.dock_id, dock_code=dock.code, dock_type=dock.dock_type,
        slot_version=state.slot_version(slot.slot_id), **kw,  # type: ignore[arg-type]
    )


def _hard_reasons(ctx: FeasibilityContext, slot: SlotRow, state: FacilityState) -> list[str]:
    r: list[str] = []
    dock = state.docks[slot.dock_id]
    u = ctx.unload
    if slot.slot_id == ctx.own_slot_id:
        r.append("OWN_CURRENT_SLOT")
    if slot.start < ctx.now:
        r.append("SLOT_IN_PAST")
    if slot.status != "OPEN":
        r.append("SLOT_BLOCKED")
    if dock.status != "ACTIVE":
        r.append("DOCK_UNAVAILABLE")
    if any(e.start < slot.end and (e.end is None or e.end > slot.start) for e in state.effective_events(dock.dock_id)):
        r.append("DOCK_EVENT_BLOCK")
    occ = state.occupancy.get(slot.slot_id)
    if occ is not None and occ.shipment_id != ctx.shipment_id:
        r.append("SLOT_HELD" if occ.status == "HELD" else "SLOT_OCCUPIED")
    opens, closes = local_bounds(state, slot)
    if slot.start < opens or slot.end > closes:
        r.append("OUTSIDE_FACILITY_HOURS")
    last_start = state.rules.get("LAST_NEW_START_TIME")
    if last_start:
        hh, mm = last_start.split(":")
        local_start = slot.start.astimezone(opens.tzinfo)
        if (local_start.hour, local_start.minute) > (int(hh), int(mm)):
            r.append("AFTER_LAST_NEW_START")
    if u.required_dock_type != "ANY" and dock.dock_type != u.required_dock_type:
        r.append("DOCK_TYPE_MISMATCH")
    if u.temperature_control_required and not dock.supports_refrigerated:
        r.append("REFRIGERATION_REQUIRED")
    heavy = state.rule_int("HEAVY_DOCK_REQUIRED_KG")
    if heavy is not None and u.load_weight_kg >= heavy and dock.dock_type != "HEAVY":
        r.append("HEAVY_DOCK_REQUIRED")
    if u.load_weight_kg > dock.max_weight_kg:
        r.append("WEIGHT_EXCEEDS_DOCK_LIMIT")
    if u.expected_unload_min > (slot.end - slot.start).total_seconds() / 60 + 0.5:
        r.append("UNLOAD_EXCEEDS_SLOT")
    if ctx.release_time + timedelta(minutes=ctx.eta_buffer_minutes) > slot.start:
        r.append("RELEASE_AFTER_SLOT_START")
    c = ctx.constraints
    if c.earliest_acceptable is not None and slot.start < c.earliest_acceptable:
        r.append("BEFORE_DRIVER_EARLIEST")
    if c.latest_acceptable is not None and slot.start > c.latest_acceptable:
        r.append("AFTER_DRIVER_LATEST")
    return r


def _labour_check(ctx: FeasibilityContext, slot: SlotRow, state: FacilityState) -> tuple[str | None, bool]:
    """Return (infeasible_reason, data_present). Coverage must span the whole slot."""
    tz = slot.start.tzinfo
    day = slot.start.astimezone(tz).date()
    day_windows = [w for w in state.labour if w.start.astimezone(tz).date() <= day <= w.end.astimezone(tz).date()]
    if not day_windows:
        return None, False
    overlapping = sorted((w for w in day_windows if w.start < slot.end and w.end > slot.start), key=lambda w: w.start)
    cursor = slot.start
    for w in overlapping:
        if w.start > cursor:
            break
        cursor = max(cursor, w.end)
    if not overlapping or cursor < slot.end:
        return "LABOUR_UNAVAILABLE", True
    if not all(w.equipment_available for w in overlapping):
        return "EQUIPMENT_UNAVAILABLE", True
    needed = state.active_overlapping(slot, exclude_shipment=ctx.shipment_id) + 1
    if min(w.crews for w in overlapping) < needed:
        return "LABOUR_UNAVAILABLE", True
    return None, True


def evaluate_slot(ctx: FeasibilityContext, slot: SlotRow, state: FacilityState) -> FeasibilityDecision:
    age = ctx.now - state.as_of
    if age > timedelta(seconds=ctx.max_state_age_seconds):
        return _decision(slot, state, ctx, "INCOMPLETE_FEASIBILITY_DATA",
                         reasons=("STALE_FACILITY_STATE",), missing=("fresh_facility_state",))
    hard = _hard_reasons(ctx, slot, state)
    if hard:
        return _decision(slot, state, ctx, "INFEASIBLE", reasons=tuple(hard))
    labour_reason, labour_known = _labour_check(ctx, slot, state)
    missing = tuple(
        m for m, ok in (("labour_availability", labour_known), ("consignee_commitment", ctx.consignee is not None)) if not ok
    )
    if missing:
        return _decision(slot, state, ctx, "INCOMPLETE_FEASIBILITY_DATA",
                         reasons=("INCOMPLETE_FEASIBILITY_DATA",), missing=missing)
    if labour_reason:
        return _decision(slot, state, ctx, "INFEASIBLE", reasons=(labour_reason,))
    c = ctx.consignee
    assert c is not None
    if c.latest_arrival is not None and slot.start > c.latest_arrival:
        if c.penalty_applies or c.exceptional:
            return _decision(slot, state, ctx, "REQUIRES_APPROVAL", reasons=("CONSIGNEE_LATEST_ARRIVAL_BREACH",))
        return _decision(slot, state, ctx, "FEASIBLE", warnings=("CONSIGNEE_DEADLINE_MISSED_NO_PENALTY",))
    return _decision(slot, state, ctx, "FEASIBLE")


def search_feasible_slots(ctx: FeasibilityContext, state: FacilityState) -> SearchResult:
    feasible, approval, incomplete = [], [], []
    rejected: Counter[str] = Counter()
    for slot in state.slots.values():
        d = evaluate_slot(ctx, slot, state)
        if d.status == "FEASIBLE":
            feasible.append(d)
        elif d.status == "REQUIRES_APPROVAL":
            approval.append(d)
        elif d.status == "INCOMPLETE_FEASIBILITY_DATA":
            incomplete.append(d)
        else:
            rejected.update(d.reasons)
    key = lambda d: (d.slot_start, d.dock_code)
    feasible.sort(key=key)
    approval.sort(key=key)
    incomplete.sort(key=key)
    if feasible:
        status: Literal["OK", "NO_SLOT", "REQUIRES_APPROVAL", "INCOMPLETE_FEASIBILITY_DATA"] = "OK"
    elif approval:
        status = "REQUIRES_APPROVAL"
    elif incomplete:
        status = "INCOMPLETE_FEASIBILITY_DATA"
    else:
        status = "NO_SLOT"
    return SearchResult(
        status=status, feasible=tuple(feasible), requires_approval=tuple(approval),
        incomplete=tuple(incomplete), rejection_counts=dict(rejected),
    )


def build_context(
    conn: sqlite3.Connection,
    shipment_id: str,
    eta: EtaEvidence,
    now: datetime,
    constraints: DriverConstraints | None = None,
    eta_buffer_minutes: int = 0,
) -> FeasibilityContext:
    s = conn.execute("select * from shipments where shipment_id=?", (shipment_id,)).fetchone()
    if s is None:
        raise KeyError(shipment_id)
    own = conn.execute(
        "select slot_id from appointments where shipment_id=? and is_current=1 "
        "and appointment_status in ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')",
        (shipment_id,),
    ).fetchone()
    return FeasibilityContext(
        shipment_id=shipment_id,
        driver_id=s["driver_id"],
        facility_id=s["destination_facility_id"],
        unload=UnloadRequirement(
            expected_unload_min=s["expected_unload_min"],
            required_dock_type=s["required_dock_type"],
            load_weight_kg=s["load_weight_kg"],
            temperature_control_required=bool(s["temperature_control_required"]),
        ),
        release_time=eta.eta,
        release_source=eta.source,
        constraints=constraints or DriverConstraints(),
        consignee=load_consignee(conn, shipment_id),
        own_slot_id=own["slot_id"] if own else None,
        now=now,
        eta_buffer_minutes=eta_buffer_minutes,
    )


def search_for_shipment(
    conn: sqlite3.Connection,
    shipment_id: str,
    eta: EtaEvidence | ClarificationNeeded | EtaConflict,
    clock: Clock,
    constraints: DriverConstraints | None = None,
) -> SearchResult:
    """Entry point used by tools: refuses to search without a usable ETA."""
    if isinstance(eta, EtaConflict):
        return SearchResult(status="ETA_CONFLICT", detail=eta.code)
    if isinstance(eta, ClarificationNeeded):
        return SearchResult(status="NEEDS_INPUT", detail=eta.code)
    ctx = build_context(conn, shipment_id, eta, clock.now(), constraints)
    state = build_facility_state(conn, ctx.facility_id, clock.now())
    return search_feasible_slots(ctx, state)


__all__ = [
    "FeasibilityContext",
    "FeasibilityDecision",
    "SearchResult",
    "build_context",
    "evaluate_slot",
    "parse_operational_timestamp",
    "search_feasible_slots",
    "search_for_shipment",
]
