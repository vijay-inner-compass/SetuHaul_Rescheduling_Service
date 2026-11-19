"""Whole-database integrity invariants for appointments, holds and history.

``check_invariants`` is read-only. An empty list means every invariant holds. The proof harness
and the property tests call it after every action.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

_ACTIVE = "('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS','HELD')"
_CURRENT_ACTIVE = "('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')"
_ENDED = "('CANCELLED','COMPLETED','NO_SHOW','REJECTED')"


@dataclass(frozen=True)
class InvariantViolation:
    name: str
    detail: str


_CHECKS: tuple[tuple[str, str], ...] = (
    ("DUPLICATE_ACTIVE_SLOT",
     f"select slot_id from appointments where appointment_status in {_ACTIVE} group by slot_id having count(*) > 1"),
    ("DUPLICATE_CURRENT_APPOINTMENT",
     f"select shipment_id from appointments where is_current=1 and appointment_status in {_CURRENT_ACTIVE} "
     f"group by shipment_id having count(*) > 1"),
    ("MULTIPLE_HOLDS_PER_SHIPMENT",
     "select shipment_id from appointments where appointment_status='HELD' group by shipment_id having count(*) > 1"),
    ("ORPHAN_HOLD_METADATA",
     "select m.appointment_id from appointment_hold_meta m left join appointments a using(appointment_id) "
     "where a.appointment_id is null"),
    ("HELD_WITHOUT_METADATA",
     "select a.appointment_id from appointments a left join appointment_hold_meta m using(appointment_id) "
     "where a.appointment_status='HELD' and m.appointment_id is null"),
    ("HOLD_METADATA_STATE_MISMATCH",
     "select a.appointment_id from appointments a join appointment_hold_meta m using(appointment_id) "
     "where (a.appointment_status='HELD' and m.released_at is not null) "
     "or (a.appointment_status<>'HELD' and m.released_at is null)"),
    ("HISTORY_WITHOUT_PARENT",
     "select e.event_id from appointment_events e left join appointments a using(appointment_id) "
     "where a.appointment_id is null"),
    ("REPLACED_APPOINTMENT_STILL_ACTIVE",
     f"select n.appointment_id from appointments n join appointments o on o.appointment_id = n.replaced_appointment_id "
     f"where n.is_current=1 and n.appointment_status in {_CURRENT_ACTIVE} and o.appointment_status not in {_ENDED}"),
)


def check_invariants(conn: sqlite3.Connection) -> list[InvariantViolation]:
    out: list[InvariantViolation] = []
    for name, sql in _CHECKS:
        for row in conn.execute(sql).fetchall():
            out.append(InvariantViolation(name, str(row[0])))
    for row in conn.execute("pragma foreign_key_check").fetchall():
        out.append(InvariantViolation("FOREIGN_KEY_VIOLATION", f"{row[0]} row {row[1]} -> {row[2]}"))
    return out
