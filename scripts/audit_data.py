"""Run the read-only data audit and print a JSON report. Never writes to the database."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from setuhaul.db import open_connection
from setuhaul.observability import InMemoryExporter, RequestContext, start_span
from setuhaul.repositories import Repository, run_audit, timestamp_pattern_audit


def main() -> int:
    ctx, exp = RequestContext.new(driver_id="AUDIT", thread_id="AUDIT"), InMemoryExporter()
    with open_connection(readonly=True) as conn, start_span("audit", ctx, exporter=exp):
        report = run_audit(conn)
        ts = timestamp_pattern_audit(conn)
        report["timestamp_audit"] = {
            "columns_checked": ts.columns_checked,
            "mixed": ts.mixed,
            "naive": ts.naive,
            "invalid": ts.invalid,
        }
        report["sample_trace"] = Repository(conn, ctx, exp).shipment_trace("SHP1006")
    report["spans"] = [
        {"name": s.name, "request_id": s.request_id, "span_id": s.span_id,
         "parent_id": s.parent_id, "status": s.status, "duration_ms": round(s.duration_ms, 2)}
        for s in exp.spans
    ]
    print(json.dumps(report, indent=2, default=str))
    bad = [k for k in ("duplicate_active_slot", "duplicate_active_appointment_per_shipment",
                       "orphan_appointments", "foreign_key_violations") if report[k]]
    return 1 if bad or ts.mixed or ts.naive or ts.invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
