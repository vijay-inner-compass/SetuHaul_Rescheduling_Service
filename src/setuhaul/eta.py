"""ETA derivation.

Rule (decision D6): "N minutes late" means planned ETA + N, unless the driver explicitly names
another baseline. "Repair takes N minutes" is not an ETA until travel time is known.
"""

# Lazy annotations.
from __future__ import annotations

# sqlite3 for the Connection type.
import sqlite3

# timedelta adds minutes to a time.
from datetime import timedelta

# The injectable clock, used when a repair starts "now".
from setuhaul.clock import Clock

# Parses stored timestamps into aware IST datetimes.
from setuhaul.db import parse_operational_timestamp

# The data shapes this module consumes and produces.
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

# The three possible outcomes of deriving an ETA: a usable ETA, a question to ask the driver, or a contradiction.
EtaResult = EtaEvidence | ClarificationNeeded | EtaConflict


def load_eta_records(
    conn: sqlite3.Connection, shipment_id: str
) -> tuple[EtaRecord | None, EtaRecord | None]:
    """Return (planned, current). Planned is shipments.original_eta_ts; current is the latest
    eta_updates row, ordered the same way as the v_latest_eta view."""
    # The planned ETA lives on the shipment row itself.
    row = conn.execute(
        "select original_eta_ts from shipments where shipment_id=?", (shipment_id,)
    ).fetchone()
    # Unknown shipment: there is neither a planned nor a current ETA.
    if row is None:
        return None, None
    # The shipment id doubles as the record id for the planned ETA.
    planned = EtaRecord(record_id=shipment_id, eta=parse_operational_timestamp(row[0]))
    # The latest declared ETA update, newest first. eta_update_id breaks ties when two updates share a timestamp.
    upd = conn.execute(
        "select eta_update_id, declared_eta_ts from eta_updates where shipment_id=? "
        "order by created_at desc, eta_update_id desc limit 1",
        (shipment_id,),
    ).fetchone()
    # None when the shipment has never had an update.
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
    # When the truck already checked in at the gate, that is a fact, not an estimate.
    gate_in = gate.gate_in_ts if gate else None
    if gate_in is not None:
        # If the driver is also reporting a delay, the two accounts contradict each other. Do not pick one:
        # return a conflict so the case goes to a human.
        if report is not None:
            return EtaConflict(
                code="CONTRADICTORY_ARRIVAL_STATE",
                detail=f"driver reports not yet arrived but gate-in recorded at {gate_in.isoformat()}",
            )
        # Gate-in with no delay report: the arrival time is the gate time, with high confidence.
        return EtaEvidence(
            eta=gate_in,
            source="GATE_CHECKIN",
            confidence="HIGH",
            baseline_type=BaselineType.GATE_IN,
        )
    # No gate evidence and no delay information at all: ask the driver rather than assume.
    if report is None:
        return ClarificationNeeded(
            code="NO_DELAY_INFORMATION",
            missing=("delay",),
            question="How late are you, or what time do you expect to arrive?",
        )

    # The driver stated the arrival time outright ("I will be there at 14:00"): use it as given.
    if report.kind is DelayKind.EXPLICIT_ETA:
        # The report validator guarantees declared_eta is present for this kind; the assert documents that.
        assert report.declared_eta is not None
        return EtaEvidence(
            eta=report.declared_eta,
            source="DRIVER_DECLARED",
            confidence=report.confidence,
            baseline_type=BaselineType.DRIVER_DECLARED,
        )

    # A breakdown: the repair duration alone is not an arrival time, the drive afterwards matters too.
    if report.kind is DelayKind.REPAIR:
        assert report.repair_minutes is not None
        # Without travel time we cannot compute an ETA, so ask for it. This is the "clarify before search" rule.
        if report.travel_minutes is None:
            return ClarificationNeeded(
                code="MISSING_TRAVEL_TIME",
                missing=("travel_minutes",),
                question="After the repair, how long will the drive to the facility take?",
            )
        # The repair is assumed to start when reported unless the driver said when it started.
        start = report.repair_started_at or clock.now()
        # ETA = start + repair + drive.
        return EtaEvidence(
            eta=start + timedelta(minutes=report.repair_minutes + report.travel_minutes),
            source="DRIVER_DECLARED",
            confidence=report.confidence,
            baseline_type=BaselineType.REPAIR_COMPLETION,
        )

    # What remains is "N minutes late"; the report validator guarantees delay_minutes is set.
    assert report.delay_minutes is not None
    # The driver may say "late compared to my latest updated ETA" rather than the original plan.
    if report.baseline is BaselineType.CURRENT_ETA:
        # They referred to an updated ETA that does not exist in our data: ask instead of guessing.
        if current is None:
            return ClarificationNeeded(
                code="MISSING_CURRENT_ETA",
                missing=("current_eta",),
                question="I have no updated ETA on file. What time were you expecting to arrive?",
            )
        base = current
    else:
        # Default rule (D6): "N minutes late" is measured from the PLANNED ETA.
        if planned is None:
            return ClarificationNeeded(
                code="MISSING_PLANNED_ETA",
                missing=("planned_eta",),
                question="I cannot find a planned arrival time for this shipment. Which shipment is it?",
            )
        base = planned
    # New ETA = baseline + delay. The baseline record id is kept so the result can be traced to its source row.
    return EtaEvidence(
        eta=base.eta + timedelta(minutes=report.delay_minutes),
        source="DRIVER_DECLARED",
        confidence=report.confidence,
        baseline_type=report.baseline,
        baseline_record_id=base.record_id,
    )
