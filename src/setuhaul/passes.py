"""The life of a Dock Access Pass: issue, re-time, revoke, cancel and expire. The raw code is returned once, never stored."""

# Lazy annotations.
from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

from setuhaul.auth import audit
from setuhaul.auth.authz import facility_ids
from setuhaul.auth.users import User
from setuhaul.clock import Clock
from setuhaul.db import immediate, parse_operational_timestamp
from setuhaul.dock_pass import hash_code, new_code
from setuhaul.identity import AuthError

# A pass in one of these states still stands for its visit.
LIVE = "('ISSUED','VALIDATED','ACTIVE','COMPLETED')"
# Minutes before the slot a truck may arrive, when the facility has no rule of its own.
DEFAULT_EARLY_MIN = 60
# How long after the slot ends the pass still works.
GRACE = timedelta(hours=4)
_APPOINTMENT = ("select a.appointment_id, sl.dock_id, sl.slot_start_ts, sl.slot_end_ts, sl.facility_id from appointments a"
                " join appointment_slots sl using(slot_id) where a.shipment_id=? and a.is_current=1"
                " and a.appointment_status in ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')")


def utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def window(conn: sqlite3.Connection, appointment: sqlite3.Row) -> tuple[str, str]:
    """From the facility's early limit before the slot starts to the grace after it ends, in UTC."""
    rule = conn.execute("select rule_value from facility_rules where facility_id=? and rule_type='CHECKIN_EARLY_LIMIT_MIN'"
                        " and active_flag=1", (appointment["facility_id"],)).fetchone()
    early = timedelta(minutes=int(rule[0]) if rule else DEFAULT_EARLY_MIN)
    return (utc(parse_operational_timestamp(appointment["slot_start_ts"]) - early),
            utc(parse_operational_timestamp(appointment["slot_end_ts"]) + GRACE))


def current_appointment(conn: sqlite3.Connection, shipment_id: str) -> sqlite3.Row | None:
    row = conn.execute(_APPOINTMENT, (shipment_id,)).fetchone()
    return row  # type: ignore[no-any-return]


def visit_for(conn: sqlite3.Connection, shipment_id: str) -> sqlite3.Row | None:
    """The shipment's visit that is not cancelled, newest first."""
    row = conn.execute("select t.* from truck_visits t join visit_shipments v using(truck_visit_id)"
                       " where v.shipment_id=? and t.status<>'CANCELLED' order by t.rowid desc limit 1", (shipment_id,)).fetchone()
    return row  # type: ignore[no-any-return]


def state_for(conn: sqlite3.Connection, shipment_id: str) -> sqlite3.Row | None:
    """The shipment's latest pass, whatever its state."""
    row = conn.execute("select p.* from dock_access_authorizations p join visit_shipments v using(truck_visit_id)"
                       " where v.shipment_id=? order by p.rowid desc limit 1", (shipment_id,)).fetchone()
    return row  # type: ignore[no-any-return]


def _shipment(conn: sqlite3.Connection, shipment_id: str, actor: User | None) -> sqlite3.Row:
    # Only shipments created in the app carry passes; out of scope looks like not existing.
    row = conn.execute("select s.* from shipments s join shipment_assignments a using(shipment_id) where s.shipment_id=?",
                       (shipment_id,)).fetchone()
    scope = None if actor is None else facility_ids(actor)
    if (row is None or row["current_status"] == "CANCELLED" or (actor is not None and actor.status != "ACTIVE")
            or (scope is not None and row["destination_facility_id"] not in scope)):
        raise AuthError("SHIPMENT_NOT_FOUND")
    return row  # type: ignore[no-any-return]


def record(conn: sqlite3.Connection, actor: User | None, action: str, shipment_id: str, clock: Clock, outcome: str = "OK",
           detail: dict[str, object] | None = None) -> None:
    audit.record(conn, actor_user_id=actor.user_id if actor else None, actor_email=actor.email if actor else None, action=action,
                 target_type="shipment", target_id=shipment_id, outcome=outcome, ip=None, detail=detail, clock=clock)


def _end_live(conn: sqlite3.Connection, truck_visit_id: str, status: str, reason: str, clock: Clock) -> int:
    revoked = utc(clock.now()) if status == "REVOKED" else None
    return conn.execute(
        f"update dock_access_authorizations set status=?, revoked_at=?, revocation_reason=?, row_version=row_version+1"
        f" where truck_visit_id=? and status in {LIVE}", (status, revoked, reason, truck_visit_id)).rowcount


def issue(conn: sqlite3.Connection, shipment_id: str, clock: Clock, actor: User | None) -> str:
    """A new code for the shipment's visit, replacing any live one. Call inside a transaction."""
    ship = _shipment(conn, shipment_id, actor)
    appointment = current_appointment(conn, shipment_id)
    if appointment is None:
        raise AuthError("NO_APPOINTMENT")
    visit = visit_for(conn, shipment_id)
    if visit is not None and visit["status"] != "EXPECTED":
        raise AuthError("VISIT_CLOSED" if visit["status"] == "CHECKED_OUT" else "VISIT_IN_PROGRESS")
    who = (ship["destination_facility_id"], ship["driver_id"], ship["vehicle_id"], ship["carrier_id"])
    if visit is None:
        visit_id = "TV-" + uuid.uuid4().hex[:12].upper()
        conn.execute("insert into truck_visits(truck_visit_id, warehouse_id, driver_id, vehicle_id, carrier_id, created_at)"
                     " values (?,?,?,?,?,?)", (visit_id, *who, utc(clock.now())))
        conn.execute("insert into visit_shipments(truck_visit_id, shipment_id) values (?,?)", (visit_id, shipment_id))
    else:
        visit_id = visit["truck_visit_id"]
        # The visit follows the shipment, which may have a new driver, vehicle or warehouse.
        conn.execute("update truck_visits set warehouse_id=?, driver_id=?, vehicle_id=?, carrier_id=?, row_version=row_version+1"
                     " where truck_visit_id=?", (*who, visit_id))
        _end_live(conn, visit_id, "REVOKED", "REISSUED", clock)
    registration = conn.execute("select registration_number from vehicles where vehicle_id=?", (ship["vehicle_id"],)).fetchone()[0]
    code = new_code()
    start, end = window(conn, appointment)
    conn.execute(
        "insert into dock_access_authorizations(authorization_id, token_hash, appointment_id, truck_visit_id, driver_id,"
        " vehicle_registration, carrier_id, warehouse_id, dock_id, valid_from_utc, valid_until_utc, status, issued_at)"
        " values (?,?,?,?,?,?,?,?,?,?,?,'ISSUED',?)",
        (str(uuid.uuid4()), hash_code(code), appointment["appointment_id"], visit_id, ship["driver_id"], registration,
         ship["carrier_id"], ship["destination_facility_id"], appointment["dock_id"], start, end, utc(clock.now())))
    record(conn, actor, "PASS_ISSUED", shipment_id, clock, detail={"truck_visit_id": visit_id})
    return code


def sync_window(conn: sqlite3.Connection, shipment_id: str, clock: Clock) -> None:
    """Move the live pass to the shipment's current appointment. Call inside the transaction that changed it."""
    visit, appointment = visit_for(conn, shipment_id), current_appointment(conn, shipment_id)
    if visit is None or appointment is None:
        return
    start, end = window(conn, appointment)
    moved = conn.execute(
        f"update dock_access_authorizations set appointment_id=?, dock_id=?, valid_from_utc=?, valid_until_utc=?,"
        f" row_version=row_version+1 where truck_visit_id=? and status in {LIVE}",
        (appointment["appointment_id"], appointment["dock_id"], start, end, visit["truck_visit_id"])).rowcount
    if moved:
        record(conn, None, "PASS_WINDOW_MOVED", shipment_id, clock, detail={"from": start, "until": end})


def revoke(conn: sqlite3.Connection, *, actor: User, shipment_id: str, reason: str, clock: Clock) -> None:
    if not reason.strip():
        raise AuthError("REASON_REQUIRED")
    with immediate(conn):
        _shipment(conn, shipment_id, actor)
        visit = visit_for(conn, shipment_id)
        if visit is None or not _end_live(conn, visit["truck_visit_id"], "REVOKED", reason.strip()[:200], clock):
            raise AuthError("NO_LIVE_PASS")
        record(conn, actor, "PASS_REVOKED", shipment_id, clock, detail={"reason": reason.strip()[:200]})


def cancel(conn: sqlite3.Connection, shipment_id: str, clock: Clock) -> None:
    """The shipment is off: its pass and any visit still expected are cancelled. Call inside a transaction."""
    visit = visit_for(conn, shipment_id)
    if visit is None:
        return
    _end_live(conn, visit["truck_visit_id"], "CANCELLED", "SHIPMENT_CANCELLED", clock)
    conn.execute("update truck_visits set status='CANCELLED', row_version=row_version+1 where truck_visit_id=? and status='EXPECTED'",
                 (visit["truck_visit_id"],))
    record(conn, None, "PASS_CANCELLED", shipment_id, clock)


def expire_due(conn: sqlite3.Connection, clock: Clock) -> int:
    """Passes never used within their window. A truck already inside is not touched."""
    now, count = clock.now(), 0
    rows = conn.execute("select p.authorization_id, p.valid_until_utc, v.shipment_id from dock_access_authorizations p"
                        " join visit_shipments v using(truck_visit_id) where p.status in ('ISSUED','VALIDATED')").fetchall()
    for r in rows:
        if parse_operational_timestamp(r["valid_until_utc"]) < now:
            conn.execute("update dock_access_authorizations set status='EXPIRED', row_version=row_version+1 where authorization_id=?",
                         (r["authorization_id"],))
            record(conn, None, "PASS_EXPIRED", r["shipment_id"], clock)
            count += 1
    return count
