"""Test helpers. Labour and consignee rows are labelled TEST_FIXTURE: the supplied snapshot has none."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime

from setuhaul.capacity import FacilityState, SlotRow
from setuhaul.db import parse_operational_timestamp as ts
from setuhaul.schemas import BaselineType, EtaEvidence

DAY = "2026-08-04"


def eta_at(hhmm: str, source: str = "DRIVER_DECLARED") -> EtaEvidence:
    return EtaEvidence(
        eta=ts(f"{DAY}T{hhmm}:00+05:30"),
        source=source,  # type: ignore[arg-type]
        confidence="HIGH",
        baseline_type=BaselineType.CURRENT_ETA,
    )


def at(hhmm: str) -> datetime:
    return ts(f"{DAY}T{hhmm}:00+05:30")


def seed_labour(
    conn: sqlite3.Connection, facility_id: str, start: str = "06:00", end: str = "22:00",
    crews: int = 8, equipment: int = 1,
) -> None:
    conn.execute(
        "insert into labour_windows (facility_id, window_start, window_end, available_crews, "
        "equipment_available, source, recorded_at) values (?,?,?,?,?,?,?)",
        (facility_id, f"{DAY}T{start}:00+05:30", f"{DAY}T{end}:00+05:30", crews, equipment,
         "TEST_FIXTURE", f"{DAY}T06:00:00+05:30"),
    )
    conn.commit()


def seed_consignee(
    conn: sqlite3.Connection, shipment_id: str, latest: str | None = None,
    penalty: int = 0, exceptional: int = 0,
) -> None:
    conn.execute(
        "insert or replace into consignee_commitments (shipment_id, latest_arrival, penalty_applies, "
        "exceptional, source, recorded_at) values (?,?,?,?,?,?)",
        (shipment_id, f"{DAY}T{latest}:00+05:30" if latest else None, penalty, exceptional,
         "TEST_FIXTURE", f"{DAY}T06:00:00+05:30"),
    )
    conn.commit()


def seed_everything_known(conn: sqlite3.Connection) -> None:
    """Complete evidence for the whole snapshot day, so hard rules can be tested in isolation."""
    for fac in ("FAC-JAI-01", "FAC-GGN-01"):
        seed_labour(conn, fac)
    for (sid,) in conn.execute("select shipment_id from shipments").fetchall():
        seed_consignee(conn, sid)


def find_slot(state: FacilityState, dock_id: str, hhmm: str) -> SlotRow:
    for s in state.slots.values():
        if s.dock_id == dock_id and s.start == at(hhmm):
            return s
    raise LookupError((dock_id, hhmm))


def with_state(state: FacilityState, **changes: object) -> FacilityState:
    return replace(state, **changes)  # type: ignore[arg-type]
