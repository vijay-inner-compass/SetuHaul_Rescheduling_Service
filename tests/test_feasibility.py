"""Phase 4: slot feasibility, ranking and the simultaneous-request policy."""

from __future__ import annotations

import sqlite3
import time
from datetime import timedelta

import pytest

from setuhaul.allocation import ALLOCATION_POLICY, allocate_fcfs, rank_options
from setuhaul.capacity import DockEvent, build_facility_state
from setuhaul.clock import FakeClock
from setuhaul.db import parse_operational_timestamp as ts
from setuhaul.feasibility import (
    FeasibilityContext,
    build_context,
    evaluate_slot,
    search_feasible_slots,
    search_for_shipment,
)
from setuhaul.schemas import ClarificationNeeded, DriverConstraints, EtaConflict
from tests.helpers import (
    at,
    eta_at,
    find_slot,
    seed_consignee,
    seed_everything_known,
    seed_labour,
    with_state,
)

NOW = "09:35"


def setup(db: sqlite3.Connection, shipment: str, eta: str, now: str = NOW, **kw: object):
    clock = FakeClock(at(now))
    ctx = build_context(db, shipment, eta_at(eta), clock.now(), **kw)  # type: ignore[arg-type]
    state = build_facility_state(db, ctx.facility_id, clock.now())
    return ctx, state


def shorter(ctx: FeasibilityContext, minutes: int) -> FeasibilityContext:
    """Hypothetical: the same shipment if its unload fitted a standard 60-minute slot."""
    return ctx.model_copy(update={"unload": ctx.unload.model_copy(update={"expected_unload_min": minutes})})


@pytest.fixture()
def known_db(db: sqlite3.Connection) -> sqlite3.Connection:
    seed_everything_known(db)
    return db


# --- dock and load compatibility -------------------------------------------------------------
def test_load_heavier_than_dock_limit_rejected(known_db: sqlite3.Connection) -> None:
    ctx, state = setup(known_db, "SHP1014", "11:25")  # 21,000 kg
    d1 = evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D1", "13:00"), state)  # D1 limit 20,000
    assert d1.status == "INFEASIBLE" and "WEIGHT_EXCEEDS_DOCK_LIMIT" in d1.reasons
    d2 = evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D2", "13:00"), state)  # D2 limit 25,000
    assert "WEIGHT_EXCEEDS_DOCK_LIMIT" not in d2.reasons
    assert d2.reasons == ("UNLOAD_EXCEEDS_SLOT",)  # 75 min unload vs 60 min slot: see decision D19
    fits = evaluate_slot(shorter(ctx, 60), find_slot(state, "DOCK-JAI-D2", "13:00"), state)
    assert fits.status == "FEASIBLE"


def test_load_exactly_at_dock_limit_is_allowed(known_db: sqlite3.Connection) -> None:
    ctx, state = setup(known_db, "SHP1009", "10:50")  # 20,000 kg, 75 min
    ctx = shorter(ctx, 60)
    assert evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D1", "13:00"), state).status == "FEASIBLE"


def test_dock_type_and_refrigeration_mismatch(known_db: sqlite3.Connection) -> None:
    ctx, state = setup(known_db, "SHP1015", "15:00")  # REEFER, temperature controlled
    d = evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D1", "16:00"), state)
    assert {"DOCK_TYPE_MISMATCH", "REFRIGERATION_REQUIRED"} <= set(d.reasons)
    assert evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D5", "16:00"), state).status == "FEASIBLE"


def test_heavy_load_needs_heavy_dock(known_db: sqlite3.Connection) -> None:
    ctx, state = setup(known_db, "SHP1016", "11:45")  # 31,000 kg HEAVY, 90 min
    bad = evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D2", "13:00"), state)
    assert {"DOCK_TYPE_MISMATCH", "HEAVY_DOCK_REQUIRED", "WEIGHT_EXCEEDS_DOCK_LIMIT"} <= set(bad.reasons)
    heavy = [s for s in state.slots.values() if s.dock_id == "DOCK-JAI-D6" and s.start >= at("12:00")]
    assert any(evaluate_slot(ctx, s, state).status == "FEASIBLE" for s in heavy)


def test_unload_longer_than_slot_rejected(known_db: sqlite3.Connection) -> None:
    ctx, state = setup(known_db, "SHP1009", "10:50")  # 75 min unload; standard slots are 60
    d = evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D2", "13:00"), state)
    assert "UNLOAD_EXCEEDS_SLOT" in d.reasons


# --- release time (ETA / gate-in) ------------------------------------------------------------
def test_slot_before_eta_rejected_and_wait_minutes_reported(known_db: sqlite3.Connection) -> None:
    ctx, state = setup(known_db, "SHP1006", "11:20")
    early = evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D2", "11:00"), state)
    assert "RELEASE_AFTER_SLOT_START" in early.reasons
    ok = evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D2", "13:00"), state)
    assert ok.status == "FEASIBLE" and ok.wait_minutes == 100


def test_eta_buffer_moves_the_boundary(known_db: sqlite3.Connection) -> None:
    ctx, state = setup(known_db, "SHP1006", "11:50", eta_buffer_minutes=15)
    d = evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D2", "12:00"), state)
    assert "RELEASE_AFTER_SLOT_START" in d.reasons


def test_gate_in_time_is_used_as_release_evidence(known_db: sqlite3.Connection) -> None:
    clock = FakeClock(at("10:30"))
    from tests.helpers import eta_at as mk

    ctx = build_context(known_db, "SHP1003", mk("10:05", source="GATE_CHECKIN"), clock.now())
    assert ctx.release_source == "GATE_CHECKIN"
    state = build_facility_state(known_db, ctx.facility_id, clock.now())
    assert evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D2", "12:00"), state).status == "FEASIBLE"


def test_driver_cannot_book_slot_in_the_past(known_db: sqlite3.Connection) -> None:
    ctx, state = setup(known_db, "SHP1006", "08:00", now="12:30")
    assert "SLOT_IN_PAST" in evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D2", "12:00"), state).reasons


# --- hours, blocks, capacity -----------------------------------------------------------------
def test_outside_facility_hours_and_last_new_start(known_db: sqlite3.Connection) -> None:
    ctx, state = setup(known_db, "SHP1006", "11:20")
    late = find_slot(state, "DOCK-JAI-D2", "21:00")
    assert evaluate_slot(ctx, late, state).status == "FEASIBLE"  # 21:00 start is allowed by the rule
    tight = with_state(state, rules={**state.rules, "LAST_NEW_START_TIME": "20:00"})
    assert "AFTER_LAST_NEW_START" in evaluate_slot(ctx, late, tight).reasons
    from datetime import time as dtime

    closed = with_state(state, close_time=dtime(21, 30))
    assert "OUTSIDE_FACILITY_HOURS" in evaluate_slot(ctx, late, closed).reasons


def test_dock_breakdown_and_maintenance_blocks(known_db: sqlite3.Connection) -> None:
    ctx, state = setup(known_db, "SHP1006", "11:20")
    d3 = evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D3", "12:00"), state)  # breakdown to 13:00
    assert d3.status == "INFEASIBLE" and "DOCK_EVENT_BLOCK" in d3.reasons
    assert evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D3", "13:00"), state).status == "FEASIBLE"


def test_capacity_reduction_event_blocks_overlapping_slot(known_db: sqlite3.Connection) -> None:
    ctx, state = setup(known_db, "SHP1006", "08:00", now="07:00")
    d = evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D2", "09:00"), state)  # reduction 08:00-09:20
    assert "DOCK_EVENT_BLOCK" in d.reasons


def test_reopened_event_closes_an_open_ended_block(known_db: sqlite3.Connection) -> None:
    ctx, state = setup(known_db, "SHP1006", "11:20")
    events = [
        *state.events,
        DockEvent("DOCK-JAI-D4", "MANUAL_BLOCK", at("10:00"), None, "forklift"),
        DockEvent("DOCK-JAI-D4", "REOPENED", at("12:30"), None, "cleared"),
    ]
    st = with_state(state, events=events)
    assert "DOCK_EVENT_BLOCK" in evaluate_slot(ctx, find_slot(st, "DOCK-JAI-D4", "12:00"), st).reasons
    assert evaluate_slot(ctx, find_slot(st, "DOCK-JAI-D4", "13:00"), st).status == "FEASIBLE"


def test_blocked_slot_status(known_db: sqlite3.Connection) -> None:
    ctx, state = setup(known_db, "SHP1015", "15:00")
    d = evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D5", "18:00"), state)
    assert "SLOT_BLOCKED" in d.reasons


def test_occupied_slot_rejected_but_cancelled_and_no_show_do_not_occupy(known_db: sqlite3.Connection) -> None:
    ctx, state = setup(known_db, "SHP1006", "11:20")
    assert "SLOT_OCCUPIED" in evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D1", "12:00"), state).reasons
    assert evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D2", "12:00"), state).status == "FEASIBLE"  # no-show


def test_own_current_slot_is_not_offered_as_a_new_option(known_db: sqlite3.Connection) -> None:
    ctx, state = setup(known_db, "SHP1013", "11:00")
    assert ctx.own_slot_id == "SLOT-JAI-018"
    assert evaluate_slot(ctx, state.slots["SLOT-JAI-018"], state).reasons == ("OWN_CURRENT_SLOT",)


def test_held_slot_is_not_offered(known_db: sqlite3.Connection) -> None:
    from setuhaul.capacity import Occupant

    ctx, state = setup(known_db, "SHP1006", "11:20")
    slot = find_slot(state, "DOCK-JAI-D2", "12:00")
    held = with_state(state, occupancy={**state.occupancy, slot.slot_id: Occupant("H1", "SHP9999", "HELD")})
    assert "SLOT_HELD" in evaluate_slot(ctx, slot, held).reasons


# --- driver constraints ----------------------------------------------------------------------
def test_driver_acceptable_window(known_db: sqlite3.Connection) -> None:
    c = DriverConstraints(earliest_acceptable=at("13:00"), latest_acceptable=at("14:00"))
    ctx, state = setup(known_db, "SHP1006", "11:20", constraints=c)
    assert "BEFORE_DRIVER_EARLIEST" in evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D2", "12:00"), state).reasons
    assert "AFTER_DRIVER_LATEST" in evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D2", "15:00"), state).reasons
    assert evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D2", "13:00"), state).status == "FEASIBLE"


# --- missing evidence is never assumed -------------------------------------------------------
def test_missing_labour_and_consignee_data_is_incomplete_not_feasible(db: sqlite3.Connection) -> None:
    ctx, state = setup(db, "SHP1006", "11:20")
    d = evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D2", "13:00"), state)
    assert d.status == "INCOMPLETE_FEASIBILITY_DATA"
    assert set(d.missing) == {"labour_availability", "consignee_commitment"}


def test_only_the_missing_piece_is_reported(db: sqlite3.Connection) -> None:
    seed_labour(db, "FAC-JAI-01")
    ctx, state = setup(db, "SHP1006", "11:20")
    assert evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D2", "13:00"), state).missing == ("consignee_commitment",)
    seed_consignee(db, "SHP1006")
    ctx, state = setup(db, "SHP1006", "11:20")
    assert evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D2", "13:00"), state).status == "FEASIBLE"


def test_hard_infeasibility_wins_over_missing_data(db: sqlite3.Connection) -> None:
    ctx, state = setup(db, "SHP1014", "11:25")
    d = evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D1", "13:00"), state)
    assert d.status == "INFEASIBLE" and d.missing == ()


def test_labour_window_must_cover_the_slot(db: sqlite3.Connection) -> None:
    seed_consignee(db, "SHP1006")
    seed_labour(db, "FAC-JAI-01", start="06:00", end="12:30")
    ctx, state = setup(db, "SHP1006", "11:20")
    assert evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D2", "12:00"), state).reasons == ("LABOUR_UNAVAILABLE",)


def test_not_enough_crews_for_concurrent_unloads(db: sqlite3.Connection) -> None:
    seed_consignee(db, "SHP1006")
    seed_labour(db, "FAC-JAI-01", crews=1)
    ctx, state = setup(db, "SHP1006", "11:20")
    # 17:00-18:00 already has SHP1015's appointment, so a second unload needs a second crew
    d = evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D2", "17:00"), state)
    assert d.reasons == ("LABOUR_UNAVAILABLE",)
    assert evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D2", "16:00"), state).status == "FEASIBLE"


def test_equipment_unavailable(db: sqlite3.Connection) -> None:
    seed_consignee(db, "SHP1006")
    seed_labour(db, "FAC-JAI-01", equipment=0)
    ctx, state = setup(db, "SHP1006", "11:20")
    assert evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D2", "13:00"), state).reasons == ("EQUIPMENT_UNAVAILABLE",)


# --- consignee commitments -------------------------------------------------------------------
def test_penalty_commitment_requires_approval(known_db: sqlite3.Connection) -> None:
    seed_consignee(known_db, "SHP1006", latest="12:00", penalty=1)
    ctx, state = setup(known_db, "SHP1006", "11:20")
    late = evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D2", "13:00"), state)
    assert late.status == "REQUIRES_APPROVAL" and late.reasons == ("CONSIGNEE_LATEST_ARRIVAL_BREACH",)
    assert evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D2", "12:00"), state).status == "FEASIBLE"


def test_exceptional_commitment_requires_approval(known_db: sqlite3.Connection) -> None:
    seed_consignee(known_db, "SHP1006", latest="12:00", exceptional=1)
    ctx, state = setup(known_db, "SHP1006", "11:20")
    assert evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D2", "13:00"), state).status == "REQUIRES_APPROVAL"


def test_missed_deadline_without_penalty_is_a_warning_not_a_block(known_db: sqlite3.Connection) -> None:
    seed_consignee(known_db, "SHP1006", latest="12:00")
    ctx, state = setup(known_db, "SHP1006", "11:20")
    d = evaluate_slot(ctx, find_slot(state, "DOCK-JAI-D2", "13:00"), state)
    assert d.status == "FEASIBLE" and d.warnings == ("CONSIGNEE_DEADLINE_MISSED_NO_PENALTY",)


# --- stale state and contradictory evidence --------------------------------------------------
def test_stale_facility_state_is_incomplete(known_db: sqlite3.Connection) -> None:
    ctx, state = setup(known_db, "SHP1006", "11:20")
    stale = with_state(state, as_of=ctx.now - timedelta(minutes=10))
    d = evaluate_slot(ctx, find_slot(stale, "DOCK-JAI-D2", "13:00"), stale)
    assert d.status == "INCOMPLETE_FEASIBILITY_DATA" and d.reasons == ("STALE_FACILITY_STATE",)


def test_contradictory_arrival_blocks_search(known_db: sqlite3.Connection) -> None:
    clock = FakeClock(at(NOW))
    out = search_for_shipment(known_db, "SHP1006", EtaConflict(code="CONTRADICTORY_ARRIVAL_STATE", detail="x"), clock)
    assert out.status == "ETA_CONFLICT" and out.feasible == ()


def test_clarification_blocks_search(known_db: sqlite3.Connection) -> None:
    clock = FakeClock(at(NOW))
    q = ClarificationNeeded(code="MISSING_TRAVEL_TIME", missing=("travel_minutes",), question="?")
    out = search_for_shipment(known_db, "SHP1006", q, clock)
    assert out.status == "NEEDS_INPUT" and out.detail == "MISSING_TRAVEL_TIME"


# --- search and ranking ----------------------------------------------------------------------
def test_search_on_unmodified_snapshot_reports_incomplete_not_ok(db: sqlite3.Connection) -> None:
    ctx, state = setup(db, "SHP1006", "11:20")
    out = search_feasible_slots(ctx, state)
    assert out.status == "INCOMPLETE_FEASIBILITY_DATA" and out.feasible == () and out.incomplete


def test_search_with_evidence_ranks_earliest_first(known_db: sqlite3.Connection) -> None:
    ctx, state = setup(known_db, "SHP1006", "11:20")
    out = search_feasible_slots(ctx, state)
    assert out.status == "OK"
    opts = rank_options(out.feasible)
    assert [o.rank for o in opts] == [1, 2, 3]
    starts = [o.decision.slot_start for o in opts]
    assert starts == sorted(starts) and starts[0] == at("12:00")
    assert all(o.decision.status == "FEASIBLE" for o in opts)


def test_rank_options_is_deterministic_and_filters_non_feasible(known_db: sqlite3.Connection) -> None:
    ctx, state = setup(known_db, "SHP1006", "11:20")
    out = search_feasible_slots(ctx, state)
    mixed = list(out.feasible) + list(out.requires_approval) + list(out.incomplete)
    assert rank_options(mixed, limit=5) == rank_options(list(reversed(mixed)), limit=5)
    assert rank_options([], limit=3) == []


def test_no_slot_status_when_nothing_fits(known_db: sqlite3.Connection) -> None:
    ctx, state = setup(known_db, "SHP1006", "23:30")
    assert search_feasible_slots(ctx, state).status == "NO_SLOT"


# --- allocation policy on the brief's contested shipments ------------------------------------
CONTESTED = ["SHP1006", "SHP1012", "SHP1013", "SHP1014", "SHP1009"]
ETAS = {"SHP1006": "11:20", "SHP1012": "11:10", "SHP1013": "11:00", "SHP1014": "11:25", "SHP1009": "10:50"}


def options_for(db: sqlite3.Connection, shipment: str, limit: int = 6, hypothetical_fit: bool = False):
    ctx, state = setup(db, shipment, ETAS[shipment])
    if hypothetical_fit:
        ctx = shorter(ctx, 60)
    return rank_options(search_feasible_slots(ctx, state).feasible, limit=limit)


def test_policy_name_is_recorded() -> None:
    assert ALLOCATION_POLICY == "FCFS_AT_HOLD_COMMIT"


def test_snapshot_reality_two_critical_loads_have_no_single_slot_option(known_db: sqlite3.Connection) -> None:
    """SHP1014 (21,000 kg, 75 min) and SHP1009 (75 min) cannot fit any single 60-minute standard slot."""
    for sid in ("SHP1014", "SHP1009"):
        ctx, state = setup(known_db, sid, ETAS[sid])
        out = search_feasible_slots(ctx, state)
        assert out.status == "NO_SLOT" and out.rejection_counts["UNLOAD_EXCEEDS_SLOT"] > 0


def test_fcfs_never_double_allocates_and_unfit_shipments_get_none(known_db: sqlite3.Connection) -> None:
    opts = {s: options_for(known_db, s) for s in CONTESTED}
    assert opts["SHP1014"] == [] and opts["SHP1009"] == []
    result = allocate_fcfs([(s, opts[s]) for s in CONTESTED])
    assert result["SHP1014"] is None and result["SHP1009"] is None  # hand-off, not a silent failure
    chosen = [result[s] for s in ("SHP1006", "SHP1012", "SHP1013")]
    assert None not in chosen and len(set(chosen)) == 3


def test_first_committer_wins_and_priority_does_not_preempt(known_db: sqlite3.Connection) -> None:
    """Hypothetical: if SHP1014 (CRITICAL) fitted a slot it would still queue behind earlier commits."""
    a = options_for(known_db, "SHP1013")
    b = options_for(known_db, "SHP1014", hypothetical_fit=True)
    assert a and b and a[0].decision.slot_id in {o.decision.slot_id for o in b}, "scenario must contend"
    normal_first = allocate_fcfs([("SHP1013", a), ("SHP1014", b)])
    critical_first = allocate_fcfs([("SHP1014", b), ("SHP1013", a)])
    assert normal_first["SHP1013"] == a[0].decision.slot_id
    assert normal_first["SHP1014"] != normal_first["SHP1013"]  # CRITICAL loses to the earlier NORMAL
    assert critical_first["SHP1014"] == b[0].decision.slot_id


def test_request_with_no_remaining_option_gets_none() -> None:
    assert allocate_fcfs([("A", []), ("B", [])]) == {"A": None, "B": None}


# --- performance -----------------------------------------------------------------------------
def test_capacity_search_p95_under_500ms(known_db: sqlite3.Connection) -> None:
    clock = FakeClock(at(NOW))
    samples = []
    for _ in range(40):
        t0 = time.perf_counter()
        search_for_shipment(known_db, "SHP1006", eta_at("11:20"), clock)
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    assert samples[int(len(samples) * 0.95) - 1] < 500


def test_context_round_trips_through_json(known_db: sqlite3.Connection) -> None:
    ctx, _ = setup(known_db, "SHP1006", "11:20")
    assert FeasibilityContext.model_validate_json(ctx.model_dump_json()) == ctx
    assert ts("2026-08-04T09:35:00+05:30") == ctx.now
