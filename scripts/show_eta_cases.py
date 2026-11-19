"""Print structured ETA outputs for the Phase 2 evidence bundle."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from setuhaul.clock import FakeClock  # noqa: E402
from setuhaul.db import open_connection  # noqa: E402
from setuhaul.db import parse_operational_timestamp as ts  # noqa: E402
from setuhaul.eta import derive_eta, load_eta_records  # noqa: E402
from setuhaul.schemas import BaselineType, DelayReport, GateEvidence  # noqa: E402

clock = FakeClock(ts("2026-08-04T09:35:00+05:30"))
with open_connection(readonly=True) as conn:
    planned, current = load_eta_records(conn, "SHP1006")
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
print(f"SHP1006 planned={planned.eta.isoformat()} current={current.eta.isoformat()} "
      f"now={clock.now().isoformat()}")
for name, (rep, gate) in cases.items():
    out = derive_eta(rep, planned, current, gate, clock)
    print(f"\n[{name}]\n  {type(out).__name__}: {out.model_dump_json()}")
