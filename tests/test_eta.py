"""Phase 2: injectable clock, domain contracts and ETA derivation semantics."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from setuhaul.clock import FakeClock, SystemClock
from setuhaul.db import open_connection
from setuhaul.db import parse_operational_timestamp as ts
from setuhaul.eta import derive_eta, load_eta_records
from setuhaul.schemas import (
    BaselineType,
    ClarificationNeeded,
    ConsigneeCommitment,
    DelayReport,
    DriverConstraints,
    EtaConflict,
    EtaEvidence,
    EtaRecord,
    GateEvidence,
    UnloadRequirement,
)

PLANNED = EtaRecord(record_id="SHP1006", eta=ts("2026-08-04T10:20:00+05:30"))
CURRENT = EtaRecord(record_id="ETA0042", eta=ts("2026-08-04T11:20:00+05:30"))
NOW = ts("2026-08-04T09:35:00+05:30")


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock(NOW)


# --- clock -------------------------------------------------------------------------------
def test_fake_clock_is_aware_and_advances_without_sleeping(clock: FakeClock) -> None:
    assert clock.now() == NOW
    assert clock.now().tzinfo is not None
    clock.advance(timedelta(minutes=3))
    assert clock.now() == NOW + timedelta(minutes=3)


def test_fake_clock_rejects_naive_time() -> None:
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 8, 4, 9, 0))


def test_system_clock_returns_aware_ist() -> None:
    assert SystemClock().now().utcoffset() == timedelta(hours=5, minutes=30)


# --- "N minutes late" --------------------------------------------------------------------
def test_late_derives_from_planned_eta_not_wall_clock(clock: FakeClock) -> None:
    out = derive_eta(DelayReport.late_by(90), PLANNED, CURRENT, None, clock)
    assert isinstance(out, EtaEvidence)
    assert out.eta == PLANNED.eta + timedelta(minutes=90)
    assert out.baseline_type is BaselineType.PLANNED_ETA
    assert out.baseline_record_id == "SHP1006"
    clock.advance(timedelta(hours=5))  # wall clock must not matter
    again = derive_eta(DelayReport.late_by(90), PLANNED, CURRENT, None, clock)
    assert isinstance(again, EtaEvidence)
    assert again.eta == out.eta


def test_late_with_explicitly_named_current_eta_baseline(clock: FakeClock) -> None:
    report = DelayReport.late_by(30, baseline=BaselineType.CURRENT_ETA)
    out = derive_eta(report, PLANNED, CURRENT, None, clock)
    assert isinstance(out, EtaEvidence)
    assert out.eta == CURRENT.eta + timedelta(minutes=30)
    assert (out.baseline_type, out.baseline_record_id) == (BaselineType.CURRENT_ETA, "ETA0042")


def test_current_eta_baseline_requested_but_absent_asks_clarification(clock: FakeClock) -> None:
    report = DelayReport.late_by(30, baseline=BaselineType.CURRENT_ETA)
    out = derive_eta(report, PLANNED, None, None, clock)
    assert isinstance(out, ClarificationNeeded)
    assert out.code == "MISSING_CURRENT_ETA"


def test_missing_planned_eta_needs_clarification(clock: FakeClock) -> None:
    out = derive_eta(DelayReport.late_by(90), None, None, None, clock)
    assert isinstance(out, ClarificationNeeded)
    assert out.code == "MISSING_PLANNED_ETA"


def test_negative_delay_is_invalid() -> None:
    with pytest.raises(ValidationError):
        DelayReport.late_by(-5)


def test_zero_delay_keeps_planned_eta(clock: FakeClock) -> None:
    out = derive_eta(DelayReport.late_by(0), PLANNED, None, None, clock)
    assert isinstance(out, EtaEvidence)
    assert out.eta == PLANNED.eta


# --- "repair takes N minutes" ------------------------------------------------------------
def test_repair_duration_without_travel_needs_clarification(clock: FakeClock) -> None:
    out = derive_eta(DelayReport.repair(90), PLANNED, CURRENT, None, clock)
    assert isinstance(out, ClarificationNeeded)
    assert out.code == "MISSING_TRAVEL_TIME"
    assert "travel_minutes" in out.missing


def test_repair_plus_travel_derives_from_repair_completion(clock: FakeClock) -> None:
    out = derive_eta(DelayReport.repair(90, travel_minutes=45), PLANNED, CURRENT, None, clock)
    assert isinstance(out, EtaEvidence)
    assert out.eta == NOW + timedelta(minutes=135)
    assert out.baseline_type is BaselineType.REPAIR_COMPLETION


def test_repair_with_stated_start_uses_start_not_now(clock: FakeClock) -> None:
    started = ts("2026-08-04T09:00:00+05:30")
    report = DelayReport.repair(90, travel_minutes=30, started_at=started)
    out = derive_eta(report, PLANNED, None, None, clock)
    assert isinstance(out, EtaEvidence)
    assert out.eta == started + timedelta(minutes=120)


# --- explicit ETA, naive input, provenance -----------------------------------------------
def test_explicit_driver_eta_is_used_as_declared(clock: FakeClock) -> None:
    declared = ts("2026-08-04T12:15:00+05:30")
    out = derive_eta(DelayReport.explicit(declared, confidence="LOW"), PLANNED, CURRENT, None, clock)
    assert isinstance(out, EtaEvidence)
    assert out.eta == declared
    assert out.baseline_type is BaselineType.DRIVER_DECLARED
    assert out.confidence == "LOW"
    assert out.source == "DRIVER_DECLARED"


def test_naive_timestamps_rejected_at_schema_boundary() -> None:
    naive = datetime(2026, 8, 4, 10, 0)
    with pytest.raises(ValidationError):
        EtaRecord(record_id="x", eta=naive)
    with pytest.raises(ValidationError):
        DelayReport.explicit(naive)


def test_confidence_and_source_preserved_on_late_report(clock: FakeClock) -> None:
    out = derive_eta(DelayReport.late_by(20, confidence="HIGH"), PLANNED, None, None, clock)
    assert isinstance(out, EtaEvidence)
    assert out.confidence == "HIGH"
    assert out.source == "DRIVER_DECLARED"


# --- gate-in evidence --------------------------------------------------------------------
def test_arrived_truck_uses_checkin_time_as_release_evidence(clock: FakeClock) -> None:
    gate = GateEvidence(gate_in_ts=ts("2026-08-04T09:20:00+05:30"))
    out = derive_eta(None, PLANNED, CURRENT, gate, clock)
    assert isinstance(out, EtaEvidence)
    assert out.eta == gate.gate_in_ts
    assert out.source == "GATE_CHECKIN"
    assert out.baseline_type is BaselineType.GATE_IN


def test_driver_claims_away_after_gate_in_is_contradictory(clock: FakeClock) -> None:
    gate = GateEvidence(gate_in_ts=ts("2026-08-04T09:20:00+05:30"))
    out = derive_eta(DelayReport.late_by(60), PLANNED, CURRENT, gate, clock)
    assert isinstance(out, EtaConflict)
    assert out.code == "CONTRADICTORY_ARRIVAL_STATE"


def test_no_report_and_no_gate_asks_what_happened(clock: FakeClock) -> None:
    out = derive_eta(None, PLANNED, CURRENT, GateEvidence(gate_in_ts=None), clock)
    assert isinstance(out, ClarificationNeeded)
    assert out.code == "NO_DELAY_INFORMATION"


# --- other contracts ---------------------------------------------------------------------
def test_driver_constraints_window_must_be_ordered() -> None:
    with pytest.raises(ValidationError):
        DriverConstraints(
            earliest_acceptable=ts("2026-08-04T13:00:00+05:30"),
            latest_acceptable=ts("2026-08-04T12:00:00+05:30"),
        )


def test_unload_requirement_and_commitment_validate() -> None:
    UnloadRequirement(expected_unload_min=45, required_dock_type="STANDARD", load_weight_kg=7800)
    with pytest.raises(ValidationError):
        UnloadRequirement(expected_unload_min=0, required_dock_type="STANDARD", load_weight_kg=1)
    c = ConsigneeCommitment(latest_arrival=ts("2026-08-04T14:00:00+05:30"), penalty_applies=True)
    assert c.penalty_applies


# --- against the supplied snapshot -------------------------------------------------------
@pytest.mark.parametrize(
    ("shipment", "minutes", "declared"),
    [("SHP1006", 60, "2026-08-04T11:20:00+05:30"), ("SHP1012", 85, "2026-08-04T11:10:00+05:30")],
)
def test_snapshot_late_reports_reproduce_declared_eta(
    shipment: str, minutes: int, declared: str, clock: FakeClock
) -> None:
    with open_connection(readonly=True) as conn:
        planned, current = load_eta_records(conn, shipment)
    assert planned is not None
    out = derive_eta(DelayReport.late_by(minutes), planned, current, None, clock)
    assert isinstance(out, EtaEvidence)
    assert out.eta == ts(declared)


def test_load_eta_records_unknown_shipment() -> None:
    with open_connection(readonly=True) as conn:
        assert load_eta_records(conn, "NOPE") == (None, None)
