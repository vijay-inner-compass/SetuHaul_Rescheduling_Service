"""Run the read-only data audit and print a JSON report. Never writes to the database."""

# Lazy annotations.
from __future__ import annotations

# json prints the report.
import json

# sys.path lets the script import the package without installing it.
import sys

# Path arithmetic.
from pathlib import Path

# Make src/ importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Connection helper.
from setuhaul.db import open_connection

# Tracing helpers, so the audit also demonstrates request ids and spans.
from setuhaul.observability import InMemoryExporter, RequestContext, start_span

# The audit queries and the shipment trace.
from setuhaul.repositories import Repository, run_audit, timestamp_pattern_audit


# Run the audit and print a JSON report. Exit code 1 if any check found a problem.
def main() -> int:
    # A request context and span collector for the audit run.
    ctx, exp = RequestContext.new(driver_id="AUDIT", thread_id="AUDIT"), InMemoryExporter()
    # A READ-ONLY connection: the audit cannot modify the database.
    with open_connection(readonly=True) as conn, start_span("audit", ctx, exporter=exp):
        # Row counts, duplicate and orphan checks, foreign keys, integrity.
        report = run_audit(conn)
        # Check every timestamp column is one shape, aware and parseable.
        ts = timestamp_pattern_audit(conn)
        # Add the timestamp results to the report.
        report["timestamp_audit"] = {
            "columns_checked": ts.columns_checked,
            "mixed": ts.mixed,
            "naive": ts.naive,
            "invalid": ts.invalid,
        }
        # Attach the full trace of one sample shipment as a worked example.
        report["sample_trace"] = Repository(conn, ctx, exp).shipment_trace("SHP1006")
    # Add the timing spans.
    report["spans"] = [
        {"name": s.name, "request_id": s.request_id, "span_id": s.span_id,
         "parent_id": s.parent_id, "status": s.status, "duration_ms": round(s.duration_ms, 2)}
        for s in exp.spans
    ]
    # Print the whole report.
    print(json.dumps(report, indent=2, default=str))
    # The checks whose results must be empty.
    bad = [k for k in ("duplicate_active_slot", "duplicate_active_appointment_per_shipment",
                       "orphan_appointments", "foreign_key_violations") if report[k]]
    # Non-zero exit if any check found a problem.
    return 1 if bad or ts.mixed or ts.naive or ts.invalid else 0


# Run main when executed as a script.
if __name__ == "__main__":
    raise SystemExit(main())
