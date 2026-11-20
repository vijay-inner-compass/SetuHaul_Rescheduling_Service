"""Print structured ETA outputs for the Phase 2 evidence bundle."""

# Lazy annotations.
from __future__ import annotations

# sys.path setup.
import sys

# Path arithmetic.
from pathlib import Path

# Make src/ importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# A fixed clock.
from setuhaul.clock import FakeClock

# Database access.
from setuhaul.db import open_connection

# Short alias for timestamp parsing.
from setuhaul.db import parse_operational_timestamp as ts

# The ETA rules being demonstrated.
from setuhaul.eta import derive_eta, load_eta_records

# Report and evidence types.
from setuhaul.schemas import BaselineType, DelayReport, GateEvidence

# 09:35 on the snapshot day.
clock = FakeClock(ts("2026-08-04T09:35:00+05:30"))
# Read-only: load shipment SHP1006's planned and current ETA.
with open_connection(readonly=True) as conn:
    # Both ETA records.
    planned, current = load_eta_records(conn, "SHP1006")
# Seven scenarios showing each ETA rule: explicit time, late-by from planned and current baselines, repair with and without travel, and gate-in with and without a contradicting report.
cases = {
    "explicit ETA 12:15": (DelayReport.explicit(ts("2026-08-04T12:15:00+05:30")), None),
    "90 min late (planned baseline)": (DelayReport.late_by(90), None),
    "90 min late (named current-ETA baseline)": (
        DelayReport.late_by(90, baseline=BaselineType.CURRENT_ETA), None),
    "repair 90 min, no travel": (DelayReport.repair(90), None),
    "repair 90 min + 45 min travel": (DelayReport.repair(90, travel_minutes=45), None),
    "gate-in 09:20, no report": (None, GateEvidence(gate_in_ts=ts("2026-08-04T09:20:00+05:30"))),
    "gate-in 09:20, driver says 60 min late": (
        DelayReport.late_by(60), GateEvidence(gate_in_ts=ts("2026-08-04T09:20:00+05:30"))),
}
# Header: the baselines and 'now'.
print(f"SHP1006 planned={planned.eta.isoformat()} current={current.eta.isoformat()} "
      f"now={clock.now().isoformat()}")
# Run each scenario.
for name, (rep, gate) in cases.items():
    # Apply the real ETA rules.
    out = derive_eta(rep, planned, current, gate, clock)
    # Print the scenario and the structured result.
    print(f"\n[{name}]\n  {type(out).__name__}: {out.model_dump_json()}")
