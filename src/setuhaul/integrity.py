"""Whole-database integrity invariants for appointments, holds and history.

``check_invariants`` is read-only. An empty list means every invariant holds. The proof harness
and the property tests call it after every action.
"""

# Lazy annotations.
from __future__ import annotations

# sqlite3 provides the Connection type.
import sqlite3

# dataclass for the violation record.
from dataclasses import dataclass

# SQL fragments reused in several checks. "Active" means the slot is occupied: a hold counts as occupying it.
_ACTIVE = "('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS','HELD')"
# "Current active" means a shipment's real appointment; a HELD row is a proposal, not its current appointment.
_CURRENT_ACTIVE = "('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')"
# Statuses in which an appointment is finished and no longer occupies anything.
_ENDED = "('CANCELLED','COMPLETED','NO_SHOW','REJECTED')"


# One broken rule: which check failed and the id of the offending row.
@dataclass(frozen=True)
class InvariantViolation:
    name: str
    detail: str


# Each check is (name, query). A query returns the ids of rows that BREAK the rule, so zero rows means the rule holds.
_CHECKS: tuple[tuple[str, str], ...] = (
    # No slot may be occupied by more than one active appointment or hold: the double-booking rule.
    ("DUPLICATE_ACTIVE_SLOT",
     f"select slot_id from appointments where appointment_status in {_ACTIVE} group by slot_id having count(*) > 1"),
    # A shipment has at most one current appointment.
    ("DUPLICATE_CURRENT_APPOINTMENT",
     (f"select shipment_id from appointments where is_current=1 and appointment_status in {_CURRENT_ACTIVE} "
     f"group by shipment_id having count(*) > 1")),
    # A shipment may hold at most one slot at a time.
    ("MULTIPLE_HOLDS_PER_SHIPMENT",
     "select shipment_id from appointments where appointment_status='HELD' group by shipment_id having count(*) > 1"),
    # Hold metadata (expiry, owner) must always belong to a real appointment row.
    ("ORPHAN_HOLD_METADATA",
     ("select m.appointment_id from appointment_hold_meta m left join appointments a using(appointment_id) "
     "where a.appointment_id is null")),
    # Every HELD appointment must carry its metadata, or it could never expire.
    ("HELD_WITHOUT_METADATA",
     ("select a.appointment_id from appointments a left join appointment_hold_meta m using(appointment_id) "
     "where a.appointment_status='HELD' and m.appointment_id is null")),
    # The row's status and its metadata must agree: held means not released, anything else means released.
    ("HOLD_METADATA_STATE_MISMATCH",
     ("select a.appointment_id from appointments a join appointment_hold_meta m using(appointment_id) "
     "where (a.appointment_status='HELD' and m.released_at is not null) "
     "or (a.appointment_status<>'HELD' and m.released_at is null)")),
    # History rows must point at an appointment that exists.
    ("HISTORY_WITHOUT_PARENT",
     ("select e.event_id from appointment_events e left join appointments a using(appointment_id) "
     "where a.appointment_id is null")),
    # A claimed hand-off case must name who claimed it.
    ("CASE_CLAIMED_WITHOUT_OWNER",
     "select case_id from escalation_cases where status='CLAIMED' and (claimed_by is null or claimed_by='')"),
    # A resolved case must say when and how it was resolved.
    ("CASE_RESOLVED_WITHOUT_RESOLUTION",
     "select case_id from escalation_cases where status='RESOLVED' and (resolved_at is null or resolution_code is null)"),
    # Every case must have at least one history event (its creation).
    ("CASE_WITHOUT_HISTORY",
     ("select c.case_id from escalation_cases c left join escalation_events e using(case_id) "
     "where e.case_id is null")),
    # Case history events must point at a case that exists.
    ("CASE_EVENT_WITHOUT_CASE",
     "select e.event_id from escalation_events e left join escalation_cases c using(case_id) where c.case_id is null"),
    # If a new appointment replaced an old one, the old one must have ended: a reschedule must not leave two live bookings.
    ("REPLACED_APPOINTMENT_STILL_ACTIVE",
     (f"select n.appointment_id from appointments n join appointments o on o.appointment_id = n.replaced_appointment_id "
     f"where n.is_current=1 and n.appointment_status in {_CURRENT_ACTIVE} and o.appointment_status not in {_ENDED}")),
)


def check_invariants(conn: sqlite3.Connection) -> list[InvariantViolation]:
    # Collects every violation found; an empty list at the end means the database is consistent.
    out: list[InvariantViolation] = []
    # Run each rule's query; every row it returns is one violation, labelled with the rule name.
    for name, sql in _CHECKS:
        for row in conn.execute(sql).fetchall():
            out.append(InvariantViolation(name, str(row[0])))
    # SQLite's own foreign-key checker reports rows whose references point nowhere (table, row, parent table).
    for row in conn.execute("pragma foreign_key_check").fetchall():
        out.append(InvariantViolation("FOREIGN_KEY_VIOLATION", f"{row[0]} row {row[1]} -> {row[2]}"))
    return out
