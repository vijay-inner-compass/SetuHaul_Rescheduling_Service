"""ETA derivation.

Rule (decision D6): "N minutes late" means planned ETA + N, unless the driver explicitly names
another baseline. "Repair takes N minutes" is not an ETA until travel time is known.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from setuhaul.clock import Clock
from setuhaul.schemas import (
    BaselineType,
    ClarificationNeeded,
    DelayKind,
    DelayReport,
    EtaConflict,
    EtaEvidence,
    EtaRecord,
    GateEvidence,
)
from setuhaul.db import parse_operational_timestamp

EtaResult = EtaEvidence | ClarificationNeeded | EtaConflict


def load_eta_records(
    conn: sqlite3.Connection, shipment_id: str
) -> tuple[EtaRecord | None, EtaRecord | None]:
    """Return (planned, current). Planned is shipments.original_eta_ts; current is the latest
    eta_updates row, ordered the same way as the v_latest_eta view."""
    row = conn.execute(
        "select original_eta_ts from shipments where shipment_id=?", (shipment_id,)
    ).fetchone()
    if row is None:
        return None, None
    planned = EtaRecord(record_id=shipment_id, eta=parse_operational_timestamp(row[0]))
    upd = conn.execute(
        "select eta_update_id, declared_eta_ts from eta_updates where shipment_id=? "
        "order by created_at desc, eta_update_id desc limit 1",
        (shipment_id,),
    ).fetchone()
    current = (
        EtaRecord(record_id=upd[0], eta=parse_operational_timestamp(upd[1])) if upd else None
    )
    return planned, current


def derive_eta(
    report: DelayReport | None,
    planned: EtaRecord | None,
    current: EtaRecord | None,
    gate: GateEvidence | None,
    clock: Clock,
) -> EtaResult:
    gate_in = gate.gate_in_ts if gate else None
    if gate_in is not None:
        if report is not None:
            return EtaConflict(
                code="CONTRADICTORY_ARRIVAL_STATE",
                detail=f"driver reports not yet arrived but gate-in recorded at {gate_in.isoformat()}",
            )
        return EtaEvidence(
            eta=gate_in,
            source="GATE_CHECKIN",
            confidence="HIGH",
            baseline_type=BaselineType.GATE_IN,
        )
    if report is None:
        return ClarificationNeeded(
            code="NO_DELAY_INFORMATION",
            missing=("delay",),
            question="How late are you, or what time do you expect to arrive?",
        )

    if report.kind is DelayKind.EXPLICIT_ETA:
        assert report.declared_eta is not None
        return EtaEvidence(
            eta=report.declared_eta,
            source="DRIVER_DECLARED",
            confidence=report.confidence,
            baseline_type=BaselineType.DRIVER_DECLARED,
        )

    if report.kind is DelayKind.REPAIR:
        assert report.repair_minutes is not None
        if report.travel_minutes is None:
            return ClarificationNeeded(
                code="MISSING_TRAVEL_TIME",
                missing=("travel_minutes",),
                question="After the repair, how long will the drive to the facility take?",
            )
        start = report.repair_started_at or clock.now()
        return EtaEvidence(
            eta=start + timedelta(minutes=report.repair_minutes + report.travel_minutes),
            source="DRIVER_DECLARED",
            confidence=report.confidence,
            baseline_type=BaselineType.REPAIR_COMPLETION,
        )

    assert report.delay_minutes is not None
    if report.baseline is BaselineType.CURRENT_ETA:
        if current is None:
            return ClarificationNeeded(
                code="MISSING_CURRENT_ETA",
                missing=("current_eta",),
                question="I have no updated ETA on file. What time were you expecting to arrive?",
            )
        base = current
    else:
        if planned is None:
            return ClarificationNeeded(
                code="MISSING_PLANNED_ETA",
                missing=("planned_eta",),
                question="I cannot find a planned arrival time for this shipment. Which shipment is it?",
            )
        base = planned
    return EtaEvidence(
        eta=base.eta + timedelta(minutes=report.delay_minutes),
        source="DRIVER_DECLARED",
        confidence=report.confidence,
        baseline_type=report.baseline,
        baseline_record_id=base.record_id,
    )
