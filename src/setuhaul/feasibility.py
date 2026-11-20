"""Slot feasibility: pure evaluation over an in-memory FacilityState.

Hard facts decide INFEASIBLE. Missing evidence (labour, consignee, stale facility state) decides
INCOMPLETE_FEASIBILITY_DATA and is never replaced by an assumption. A penalty or exceptional
consignee commitment decides REQUIRES_APPROVAL rather than a silent choice.
"""

# Lazy annotations.
from __future__ import annotations

# hashlib builds the decision digest.
import hashlib

# sqlite3 for the Connection type.
import sqlite3

# Counter tallies why slots were rejected.
from collections import Counter

# datetime/timedelta for times and buffers.
from datetime import datetime, timedelta

# Literal restricts status strings.
from typing import Literal

# Frozen validated models.
from pydantic import BaseModel, ConfigDict

# The in-memory facility snapshot and its loaders.
from setuhaul.capacity import (
    FacilityState,
    SlotRow,
    build_facility_state,
    load_consignee,
    local_bounds,
)

# The injectable clock.
from setuhaul.clock import Clock

# Re-exported below in __all__.
from setuhaul.db import parse_operational_timestamp

# Contracts for the inputs.
from setuhaul.schemas import (
    Aware,
    ClarificationNeeded,
    ConsigneeCommitment,
    DriverConstraints,
    EtaConflict,
    EtaEvidence,
    UnloadRequirement,
)

# The four possible verdicts on a slot.
Status = Literal["FEASIBLE", "INFEASIBLE", "REQUIRES_APPROVAL", "INCOMPLETE_FEASIBILITY_DATA"]
# A facility snapshot older than this is not trusted for a decision.
DEFAULT_MAX_STATE_AGE = timedelta(minutes=5)


class FeasibilityContext(BaseModel):
    """Everything about the requesting shipment that decides slot eligibility."""

    model_config = ConfigDict(frozen=True)

    shipment_id: str
    driver_id: str
    facility_id: str
    # What the unloading needs (dock type, weight, duration, refrigeration).
    unload: UnloadRequirement
    # When the truck can start unloading (its ETA), and where that figure came from.
    release_time: Aware
    release_source: str
    # The driver's own acceptable time window, if any.
    constraints: DriverConstraints = DriverConstraints()
    # The consignee's delivery commitment; None means unknown.
    consignee: ConsigneeCommitment | None = None
    # The slot the shipment currently holds, so it is not offered back as an "alternative".
    own_slot_id: str | None = None
    # The decision moment.
    now: Aware
    # Extra minutes added to the ETA as a safety margin.
    eta_buffer_minutes: int = 0
    max_state_age_seconds: int = int(DEFAULT_MAX_STATE_AGE.total_seconds())

    def with_now(self, now: datetime) -> FeasibilityContext:
        # A copy of this context at a different moment; used to re-check an old option set against current time.
        return self.model_copy(update={"now": now})


# The verdict on one slot, with the reasons.
class FeasibilityDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    slot_id: str
    status: Status
    # Why a slot was rejected or needs approval (stable codes such as DOCK_TYPE_MISMATCH).
    reasons: tuple[str, ...] = ()
    # Evidence that was missing (for INCOMPLETE verdicts).
    missing: tuple[str, ...] = ()
    # Non-blocking cautions on a feasible slot.
    warnings: tuple[str, ...] = ()
    # Minutes between the truck's release time and the slot start.
    wait_minutes: int | None = None
    slot_start: Aware
    slot_end: Aware
    dock_id: str
    dock_code: str
    dock_type: str
    # The slot's fingerprint at decision time; compared later to detect staleness.
    slot_version: str

    @property
    def digest(self) -> str:
        # A short hash of the whole decision (times excluded), stored with each shown option to detect later changes.
        blob = self.model_dump_json(exclude={"slot_start", "slot_end"})
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


# The outcome of searching every slot at a facility.
class SearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal[
        "OK", "NO_SLOT", "REQUIRES_APPROVAL", "INCOMPLETE_FEASIBILITY_DATA", "NEEDS_INPUT", "ETA_CONFLICT"
    ]
    # Slots that can be offered now.
    feasible: tuple[FeasibilityDecision, ...] = ()
    # Slots that would need a human to approve.
    requires_approval: tuple[FeasibilityDecision, ...] = ()
    # Slots we could not judge because evidence is missing.
    incomplete: tuple[FeasibilityDecision, ...] = ()
    # How often each rejection reason occurred, for diagnosis and hand-off context.
    rejection_counts: dict[str, int] = {}
    detail: str | None = None


def _decision(slot: SlotRow, state: FacilityState, ctx: FeasibilityContext, status: Status, **kw: object) -> FeasibilityDecision:
    dock = state.docks[slot.dock_id]
    # The earliest the truck can start, including the safety buffer.
    release = ctx.release_time + timedelta(minutes=ctx.eta_buffer_minutes)
    # Whole minutes from release to slot start (floor division; negative would mean the slot starts before arrival).
    wait = int((slot.start - release).total_seconds() // 60)
    # Build the verdict, stamping it with the slot's current fingerprint.
    return FeasibilityDecision(
        slot_id=slot.slot_id, status=status, wait_minutes=wait, slot_start=slot.start, slot_end=slot.end,
        dock_id=dock.dock_id, dock_code=dock.code, dock_type=dock.dock_type,
        slot_version=state.slot_version(slot.slot_id), **kw,  # type: ignore[arg-type]
    )


def _hard_reasons(ctx: FeasibilityContext, slot: SlotRow, state: FacilityState) -> list[str]:
    # Every hard rule is checked and ALL failures are collected (not just the first), so diagnosis shows the whole picture.
    r: list[str] = []
    dock = state.docks[slot.dock_id]
    u = ctx.unload
    # Do not "move" a shipment to the slot it already has.
    if slot.slot_id == ctx.own_slot_id:
        r.append("OWN_CURRENT_SLOT")
    # The slot has already started.
    if slot.start < ctx.now:
        r.append("SLOT_IN_PAST")
    # A planner blocked the slot.
    if slot.status != "OPEN":
        r.append("SLOT_BLOCKED")
    # The dock itself is out of service.
    if dock.status != "ACTIVE":
        r.append("DOCK_UNAVAILABLE")
    # A maintenance/breakdown event overlaps the slot (interval-overlap test; end None means still blocked).
    if any(e.start < slot.end and (e.end is None or e.end > slot.start) for e in state.effective_events(dock.dock_id)):
        r.append("DOCK_EVENT_BLOCK")
    # Someone else already holds or has booked the slot. HELD is reported separately from booked.
    occ = state.occupancy.get(slot.slot_id)
    if occ is not None and occ.shipment_id != ctx.shipment_id:
        r.append("SLOT_HELD" if occ.status == "HELD" else "SLOT_OCCUPIED")
    # The slot must lie inside the facility's opening hours on its local date.
    opens, closes = local_bounds(state, slot)
    if slot.start < opens or slot.end > closes:
        r.append("OUTSIDE_FACILITY_HOURS")
    # Some facilities refuse new starts after a cut-off time (rule LAST_NEW_START_TIME, "HH:MM").
    last_start = state.rules.get("LAST_NEW_START_TIME")
    if last_start:
        hh, mm = last_start.split(":")
        local_start = slot.start.astimezone(opens.tzinfo)
        # Tuple comparison: (hour, minute) later than the cut-off means too late.
        if (local_start.hour, local_start.minute) > (int(hh), int(mm)):
            r.append("AFTER_LAST_NEW_START")
    # The load needs a specific dock type that this dock is not.
    if u.required_dock_type != "ANY" and dock.dock_type != u.required_dock_type:
        r.append("DOCK_TYPE_MISMATCH")
    # Refrigerated loads need a dock that supports refrigeration.
    if u.temperature_control_required and not dock.supports_refrigerated:
        r.append("REFRIGERATION_REQUIRED")
    # Very heavy loads (at or above the facility's threshold) must use a HEAVY dock.
    heavy = state.rule_int("HEAVY_DOCK_REQUIRED_KG")
    if heavy is not None and u.load_weight_kg >= heavy and dock.dock_type != "HEAVY":
        r.append("HEAVY_DOCK_REQUIRED")
    # The load is heavier than the dock's physical limit.
    if u.load_weight_kg > dock.max_weight_kg:
        r.append("WEIGHT_EXCEEDS_DOCK_LIMIT")
    # Unloading takes longer than the slot (half a minute of tolerance absorbs rounding).
    if u.expected_unload_min > (slot.end - slot.start).total_seconds() / 60 + 0.5:
        r.append("UNLOAD_EXCEEDS_SLOT")
    # The truck cannot be there by the time the slot starts.
    if ctx.release_time + timedelta(minutes=ctx.eta_buffer_minutes) > slot.start:
        r.append("RELEASE_AFTER_SLOT_START")
    # The driver's own acceptable window.
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
    # Labour windows that touch the slot's calendar day. None at all means we have no labour data for that day.
    day_windows = [w for w in state.labour if w.start.astimezone(tz).date() <= day <= w.end.astimezone(tz).date()]
    if not day_windows:
        # (no verdict, data missing): the caller turns this into INCOMPLETE rather than assuming labour exists.
        return None, False
    # Windows that actually overlap the slot, earliest first.
    overlapping = sorted((w for w in day_windows if w.start < slot.end and w.end > slot.start), key=lambda w: w.start)
    # Walk along the slot with a cursor, checking the windows leave no gap.
    cursor = slot.start
    for w in overlapping:
        # A window starting after the cursor leaves an uncovered gap.
        if w.start > cursor:
            break
        cursor = max(cursor, w.end)
    # Not fully covered by labour windows.
    if not overlapping or cursor < slot.end:
        return "LABOUR_UNAVAILABLE", True
    # Labour exists but the equipment (forklifts, etc.) is not available throughout.
    if not all(w.equipment_available for w in overlapping):
        return "EQUIPMENT_UNAVAILABLE", True
    # Crews needed = other active appointments overlapping this slot, plus this one.
    needed = state.active_overlapping(slot, exclude_shipment=ctx.shipment_id) + 1
    # The smallest crew count across the slot must cover that.
    if min(w.crews for w in overlapping) < needed:
        return "LABOUR_UNAVAILABLE", True
    return None, True


def evaluate_slot(ctx: FeasibilityContext, slot: SlotRow, state: FacilityState) -> FeasibilityDecision:
    # An old snapshot may no longer reflect reality: report missing evidence rather than decide on it.
    age = ctx.now - state.as_of
    if age > timedelta(seconds=ctx.max_state_age_seconds):
        return _decision(slot, state, ctx, "INCOMPLETE_FEASIBILITY_DATA",
                         reasons=("STALE_FACILITY_STATE",), missing=("fresh_facility_state",))
    # Hard facts first: if any apply, the slot is out.
    hard = _hard_reasons(ctx, slot, state)
    if hard:
        return _decision(slot, state, ctx, "INFEASIBLE", reasons=tuple(hard))
    # Now the soft inputs: labour data and consignee commitment.
    labour_reason, labour_known = _labour_check(ctx, slot, state)
    # Which required evidence is absent.
    missing = tuple(
        m for m, ok in (("labour_availability", labour_known), ("consignee_commitment", ctx.consignee is not None)) if not ok
    )
    # Missing evidence is never replaced by an assumption: the verdict is INCOMPLETE.
    if missing:
        return _decision(slot, state, ctx, "INCOMPLETE_FEASIBILITY_DATA",
                         reasons=("INCOMPLETE_FEASIBILITY_DATA",), missing=missing)
    # Labour or equipment is known to be insufficient.
    if labour_reason:
        return _decision(slot, state, ctx, "INFEASIBLE", reasons=(labour_reason,))
    c = ctx.consignee
    # Guaranteed by the "missing" check above; the assert tells the type checker.
    assert c is not None
    # The slot starts after the consignee's latest acceptable arrival.
    if c.latest_arrival is not None and slot.start > c.latest_arrival:
        # If missing the deadline has a penalty or the delivery is exceptional, a human must decide.
        if c.penalty_applies or c.exceptional:
            return _decision(slot, state, ctx, "REQUIRES_APPROVAL", reasons=("CONSIGNEE_LATEST_ARRIVAL_BREACH",))
        # No penalty: feasible, but flagged so the offer can mention it.
        return _decision(slot, state, ctx, "FEASIBLE", warnings=("CONSIGNEE_DEADLINE_MISSED_NO_PENALTY",))
    # Everything passes.
    return _decision(slot, state, ctx, "FEASIBLE")


def search_feasible_slots(ctx: FeasibilityContext, state: FacilityState) -> SearchResult:
    feasible, approval, incomplete = [], [], []
    # Tally of rejection reasons across all infeasible slots.
    rejected: Counter[str] = Counter()
    # Judge every slot at the facility.
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
    # Deterministic order: earliest start, then dock code.
    key = lambda d: (d.slot_start, d.dock_code)
    feasible.sort(key=key)
    approval.sort(key=key)
    incomplete.sort(key=key)
    # The overall status, in priority order: usable options, else approvable ones, else missing data, else nothing.
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
    # The shipment's own facts.
    s = conn.execute("select * from shipments where shipment_id=?", (shipment_id,)).fetchone()
    if s is None:
        raise KeyError(shipment_id)
    # The slot it currently holds, if any, so that slot is excluded from the options.
    own = conn.execute(
        "select slot_id from appointments where shipment_id=? and is_current=1 "
        "and appointment_status in ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')",
        (shipment_id,),
    ).fetchone()
    # Assemble the context from the shipment row, the ETA, the consignee table and the current booking.
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
    # A contradictory ETA must not be searched on; report it so the case goes to a human.
    if isinstance(eta, EtaConflict):
        return SearchResult(status="ETA_CONFLICT", detail=eta.code)
    # A missing fact must be asked for first; report that no search happened.
    if isinstance(eta, ClarificationNeeded):
        return SearchResult(status="NEEDS_INPUT", detail=eta.code)
    # A usable ETA: build the context and a fresh snapshot of the facility, then search it.
    ctx = build_context(conn, shipment_id, eta, clock.now(), constraints)
    state = build_facility_state(conn, ctx.facility_id, clock.now())
    return search_feasible_slots(ctx, state)


# Names other modules may import from here. parse_operational_timestamp is re-exported for convenience.
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
