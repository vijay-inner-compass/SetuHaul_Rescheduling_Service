"""Read repositories and named audit queries over the supplied snapshot."""

# Lazy annotations.
from __future__ import annotations

# re inspects column names and timestamp shapes.
import re

# sqlite3 for the Connection type.
import sqlite3

# Counter tallies timestamp shapes.
from collections import Counter

# dataclass/field for the audit result.
from dataclasses import dataclass, field

# Any for loosely typed rows.
from typing import Any

# Timestamp parsing and its error type.
from setuhaul.db import TimestampError, parse_operational_timestamp

# Tracing helpers used by Repository.
from setuhaul.observability import InMemoryExporter, RequestContext, start_span

# Statuses meaning "a real, live appointment" (a HELD proposal is deliberately not included here).
_ACTIVE = "('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')"

# Named data-quality questions asked of the supplied database. Each query returns the rows that are WRONG.
AUDIT_QUERIES: dict[str, str] = {
    # Placeholder: row counts are computed in run_audit (they need a query per table).
    "row_counts": "",  # computed in run_audit
    # Two live appointments on one slot would be a double booking.
    "duplicate_active_slot": f"""
        select slot_id, count(*) n from appointments
        where appointment_status in {_ACTIVE} group by slot_id having n > 1""",
    # A shipment should have at most one current live appointment.
    "duplicate_active_appointment_per_shipment": f"""
        select shipment_id, count(*) n from appointments
        where is_current = 1 and appointment_status in {_ACTIVE}
        group by shipment_id having n > 1""",
    # Appointments pointing at a shipment or slot that does not exist.
    "orphan_appointments": """
        select a.appointment_id from appointments a
        left join shipments s on s.shipment_id = a.shipment_id
        left join appointment_slots sl on sl.slot_id = a.slot_id
        where s.shipment_id is null or sl.slot_id is null""",
    # Shipments lacking the facts needed to search for a slot (weight, unload time, dock type, ETA).
    "shipments_missing_feasibility_inputs": """
        select shipment_id from shipments
        where load_weight_kg is null or expected_unload_min is null
           or required_dock_type is null or original_eta_ts is null""",
    # Live appointments for shipments that are already cancelled or completed.
    "active_appointments_for_inactive_shipments": f"""
        select a.appointment_id, s.current_status from appointments a
        join shipments s on s.shipment_id = a.shipment_id
        where a.appointment_status in {_ACTIVE}
          and s.current_status in ('CANCELLED','COMPLETED')""",
    # A gate-in time with no arrival state is incomplete evidence.
    "gate_in_without_arrival_state": """
        select shipment_id from facility_checkins
        where gate_in_ts is not null and arrival_state is null""",
    # Two slots on the same dock whose time ranges overlap (the standard interval-overlap test).
    "slots_overlapping_dock": """
        select a.slot_id, b.slot_id other from appointment_slots a
        join appointment_slots b on a.dock_id = b.dock_id and a.slot_id < b.slot_id
         and a.slot_start_ts < b.slot_end_ts and b.slot_start_ts < a.slot_end_ts""",
}

# Column names ending in _ts or _at are timestamps by this project's naming convention.
_TIME_COL = re.compile(r"(_ts|_at)$")


def run_audit(conn: sqlite3.Connection) -> dict[str, Any]:
    # All user tables (skipping SQLite's internal ones).
    tables = [
        r[0]
        for r in conn.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
        )
    ]
    # Start the report with a row count per table.
    report: dict[str, Any] = {
        "row_counts": {t: conn.execute(f'select count(*) from "{t}"').fetchone()[0] for t in tables}
    }
    # Run every named audit query that has SQL; an empty list means that check found nothing wrong.
    for name, sql in AUDIT_QUERIES.items():
        if sql:
            report[name] = [tuple(r) for r in conn.execute(sql).fetchall()]
    # SQLite's own structural checks.
    report["foreign_key_violations"] = [tuple(r) for r in conn.execute("pragma foreign_key_check")]
    report["integrity_check"] = [r[0] for r in conn.execute("pragma integrity_check")]
    return report


# Result of scanning every timestamp column in the database.
@dataclass
class TimestampAudit:
    columns_checked: int = 0
    # Columns holding more than one textual shape (a sign of inconsistent data entry).
    mixed: dict[str, dict[str, int]] = field(default_factory=dict)
    # Columns with values that lack a timezone offset, with a count.
    naive: dict[str, int] = field(default_factory=dict)
    # Columns with values that cannot be parsed at all, with a count.
    invalid: dict[str, int] = field(default_factory=dict)


def timestamp_pattern_audit(conn: sqlite3.Connection) -> TimestampAudit:
    """Check every *_ts / *_at column: one shape per column, all aware, all parseable."""
    result = TimestampAudit()
    # Every user table.
    tables = [
        r[0]
        for r in conn.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
        )
    ]
    for table in tables:
        # Column names from the table definition (index 1 is the name).
        for col in [r[1] for r in conn.execute(f'pragma table_info("{table}")')]:
            # Only timestamp-looking columns are audited.
            if not _TIME_COL.search(col):
                continue
            result.columns_checked += 1
            key = f"{table}.{col}"
            # Tally of "shapes": each digit replaced by 9, so "2026-08-04T09:35:00+05:30" becomes "9999-99-99T99:99:99+99:99".
            shapes: Counter[str] = Counter()
            for (value,) in conn.execute(f'select "{col}" from "{table}" where "{col}" is not null'):
                shapes[re.sub(r"\d", "9", value)] += 1
                try:
                    parse_operational_timestamp(value)
                except TimestampError as exc:
                    # Our parser's message says "naive" for missing offsets; anything else counts as invalid.
                    bucket = result.naive if "naive" in str(exc) else result.invalid
                    bucket[key] = bucket.get(key, 0) + 1
            # More than one shape in a column means mixed formats.
            if len(shapes) > 1:
                result.mixed[key] = dict(shapes)
    return result


class Repository:
    """Read-only access used by services from Phase 2 onward."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        context: RequestContext | None = None,
        exporter: InMemoryExporter | None = None,
    ) -> None:
        self._c = conn
        # A request context for tracing; a fresh one if the caller did not pass any.
        self._ctx = context or RequestContext.new()
        self._exporter = exporter

    def _one(self, sql: str, *args: Any) -> dict[str, Any] | None:
        # Run a query expected to return at most one row and return it as a plain dict (or None).
        row = self._c.execute(sql, args).fetchone()
        return dict(row) if row else None

    def get_shipment(self, shipment_id: str) -> dict[str, Any] | None:
        return self._one("select * from shipments where shipment_id=?", shipment_id)

    def shipment_trace(self, shipment_id: str) -> dict[str, Any]:
        # Wrap the lookup in a timed span so slow traces show up in observability.
        with start_span("repository.shipment_trace", self._ctx, exporter=self._exporter):
            return self._shipment_trace(shipment_id)

    def _shipment_trace(self, shipment_id: str) -> dict[str, Any]:
        s = self.get_shipment(shipment_id)
        # An unknown shipment is a programming or data error here, not a normal case.
        if s is None:
            raise KeyError(shipment_id)
        # The shipment's current live appointment, if any.
        appt = self._one(
            f"select * from appointments where shipment_id=? and is_current=1 "
            f"and appointment_status in {_ACTIVE}",
            shipment_id,
        )
        # The slot that appointment occupies.
        slot = self._one("select * from appointment_slots where slot_id=?", appt["slot_id"]) if appt else None
        # Everything known about the shipment, gathered in one dictionary for audits and the "trace" demo.
        return {
            "shipment": s,
            "driver": self._one("select * from drivers where driver_id=?", s["driver_id"]),
            "vehicle": self._one("select * from vehicles where vehicle_id=?", s["vehicle_id"]),
            # v_latest_eta is a view picking the newest ETA update per shipment.
            "eta": self._one("select * from v_latest_eta where shipment_id=?", shipment_id),
            "facility": self._one(
                "select * from facilities where facility_id=?", s["destination_facility_id"]
            ),
            "appointment": appt,
            # Every appointment this shipment ever had, oldest booking first.
            "appointment_history": [
                dict(r)
                for r in self._c.execute(
                    "select * from appointments where shipment_id=? order by booked_at",
                    (shipment_id,),
                )
            ],
            "slot": slot,
            "dock": self._one("select * from docks where dock_id=?", slot["dock_id"]) if slot else None,
            "checkin": self._one("select * from facility_checkins where shipment_id=?", shipment_id),
            # Past driver-reported exceptions, oldest first.
            "exceptions": [
                dict(r)
                for r in self._c.execute(
                    "select * from driver_exceptions where shipment_id=? order by reported_at",
                    (shipment_id,),
                )
            ],
        }
