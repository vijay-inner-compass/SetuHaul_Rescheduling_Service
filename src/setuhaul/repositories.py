"""Read repositories and named audit queries over the supplied snapshot."""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from setuhaul.db import TimestampError, parse_operational_timestamp
from setuhaul.observability import InMemoryExporter, RequestContext, start_span

_ACTIVE = "('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')"

AUDIT_QUERIES: dict[str, str] = {
    "row_counts": "",  # computed in run_audit
    "duplicate_active_slot": f"""
        select slot_id, count(*) n from appointments
        where appointment_status in {_ACTIVE} group by slot_id having n > 1""",
    "duplicate_active_appointment_per_shipment": f"""
        select shipment_id, count(*) n from appointments
        where is_current = 1 and appointment_status in {_ACTIVE}
        group by shipment_id having n > 1""",
    "orphan_appointments": """
        select a.appointment_id from appointments a
        left join shipments s on s.shipment_id = a.shipment_id
        left join appointment_slots sl on sl.slot_id = a.slot_id
        where s.shipment_id is null or sl.slot_id is null""",
    "shipments_missing_feasibility_inputs": """
        select shipment_id from shipments
        where load_weight_kg is null or expected_unload_min is null
           or required_dock_type is null or original_eta_ts is null""",
    "active_appointments_for_inactive_shipments": f"""
        select a.appointment_id, s.current_status from appointments a
        join shipments s on s.shipment_id = a.shipment_id
        where a.appointment_status in {_ACTIVE}
          and s.current_status in ('CANCELLED','COMPLETED')""",
    "gate_in_without_arrival_state": """
        select shipment_id from facility_checkins
        where gate_in_ts is not null and arrival_state is null""",
    "slots_overlapping_dock": """
        select a.slot_id, b.slot_id other from appointment_slots a
        join appointment_slots b on a.dock_id = b.dock_id and a.slot_id < b.slot_id
         and a.slot_start_ts < b.slot_end_ts and b.slot_start_ts < a.slot_end_ts""",
}

_TIME_COL = re.compile(r"(_ts|_at)$")


def run_audit(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = [
        r[0]
        for r in conn.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
        )
    ]
    report: dict[str, Any] = {
        "row_counts": {t: conn.execute(f'select count(*) from "{t}"').fetchone()[0] for t in tables}
    }
    for name, sql in AUDIT_QUERIES.items():
        if sql:
            report[name] = [tuple(r) for r in conn.execute(sql).fetchall()]
    report["foreign_key_violations"] = [tuple(r) for r in conn.execute("pragma foreign_key_check")]
    report["integrity_check"] = [r[0] for r in conn.execute("pragma integrity_check")]
    return report


@dataclass
class TimestampAudit:
    columns_checked: int = 0
    mixed: dict[str, dict[str, int]] = field(default_factory=dict)
    naive: dict[str, int] = field(default_factory=dict)
    invalid: dict[str, int] = field(default_factory=dict)


def timestamp_pattern_audit(conn: sqlite3.Connection) -> TimestampAudit:
    """Check every *_ts / *_at column: one shape per column, all aware, all parseable."""
    result = TimestampAudit()
    tables = [
        r[0]
        for r in conn.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
        )
    ]
    for table in tables:
        for col in [r[1] for r in conn.execute(f'pragma table_info("{table}")')]:
            if not _TIME_COL.search(col):
                continue
            result.columns_checked += 1
            key = f"{table}.{col}"
            shapes: Counter[str] = Counter()
            for (value,) in conn.execute(f'select "{col}" from "{table}" where "{col}" is not null'):
                shapes[re.sub(r"\d", "9", value)] += 1
                try:
                    parse_operational_timestamp(value)
                except TimestampError as exc:
                    bucket = result.naive if "naive" in str(exc) else result.invalid
                    bucket[key] = bucket.get(key, 0) + 1
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
        self._ctx = context or RequestContext.new()
        self._exporter = exporter

    def _one(self, sql: str, *args: Any) -> dict[str, Any] | None:
        row = self._c.execute(sql, args).fetchone()
        return dict(row) if row else None

    def get_shipment(self, shipment_id: str) -> dict[str, Any] | None:
        return self._one("select * from shipments where shipment_id=?", shipment_id)

    def shipment_trace(self, shipment_id: str) -> dict[str, Any]:
        with start_span("repository.shipment_trace", self._ctx, exporter=self._exporter):
            return self._shipment_trace(shipment_id)

    def _shipment_trace(self, shipment_id: str) -> dict[str, Any]:
        s = self.get_shipment(shipment_id)
        if s is None:
            raise KeyError(shipment_id)
        appt = self._one(
            f"select * from appointments where shipment_id=? and is_current=1 "
            f"and appointment_status in {_ACTIVE}",
            shipment_id,
        )
        slot = self._one("select * from appointment_slots where slot_id=?", appt["slot_id"]) if appt else None
        return {
            "shipment": s,
            "driver": self._one("select * from drivers where driver_id=?", s["driver_id"]),
            "vehicle": self._one("select * from vehicles where vehicle_id=?", s["vehicle_id"]),
            "eta": self._one("select * from v_latest_eta where shipment_id=?", shipment_id),
            "facility": self._one(
                "select * from facilities where facility_id=?", s["destination_facility_id"]
            ),
            "appointment": appt,
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
            "exceptions": [
                dict(r)
                for r in self._c.execute(
                    "select * from driver_exceptions where shipment_id=? order by reported_at",
                    (shipment_id,),
                )
            ],
        }
