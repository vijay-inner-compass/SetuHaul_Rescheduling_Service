"""The life of a Dock Access Pass: issue, re-time, revoke, cancel and expire. The raw code is returned once, never stored."""

# Lazy annotations.
from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

from setuhaul.auth import audit
from setuhaul.auth.authz import facility_ids
from setuhaul.auth.telegram import TelegramClient, TelegramSendError, bound_chat
from setuhaul.auth.users import User
from setuhaul.clock import Clock
from setuhaul.db import immediate, parse_operational_timestamp
from setuhaul.dock_pass import hash_code, new_code
from setuhaul.identity import AuthError
from setuhaul.language import say

# A pass in one of these states still stands for its visit.
LIVE = "('ISSUED','VALIDATED','ACTIVE','COMPLETED')"
# Minutes before the slot a truck may arrive, when the facility has no rule of its own.
DEFAULT_EARLY_MIN = 60
# How long after the slot ends the pass still works.
GRACE = timedelta(hours=4)
# The wait before a replacement that could not be sent is tried again.
RETRY_AFTER = timedelta(seconds=60)
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


def on_site(conn: sqlite3.Connection, shipment_id: str) -> bool:
    """The truck has passed the gate (or already left): its booking may no longer be moved or cancelled."""
    return conn.execute("select 1 from truck_visits t join visit_shipments v using(truck_visit_id) where v.shipment_id=?"
                        " and t.status in ('CHECKED_IN','OPERATIONS_COMPLETE','CHECKED_OUT')", (shipment_id,)).fetchone() is not None


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
    # A pass that ran out is brought back when the booking moves ahead again: the driver already holds the code.
    if visit["status"] == "EXPECTED" and parse_operational_timestamp(end) > clock.now():
        conn.execute("update dock_access_authorizations set status='ISSUED' where status='EXPIRED' and rowid=(select max(q.rowid)"
                     " from dock_access_authorizations q where q.truck_visit_id=?)", (visit["truck_visit_id"],))
    moved = conn.execute(
        f"update dock_access_authorizations set appointment_id=?, dock_id=?, valid_from_utc=?, valid_until_utc=?,"
        f" row_version=row_version+1 where truck_visit_id=? and status in {LIVE}",
        (appointment["appointment_id"], appointment["dock_id"], start, end, visit["truck_visit_id"])).rowcount
    if moved:
        record(conn, None, "PASS_WINDOW_MOVED", shipment_id, clock, detail={"from": start, "until": end})


def move_warehouse(conn: sqlite3.Connection, shipment_id: str, clock: Clock) -> None:
    """The shipment now goes elsewhere: the visit follows and the old code stops working. Call inside the transaction.

    A new code is sent by `send_pending`, from wherever there is a Telegram client.
    """
    visit = visit_for(conn, shipment_id)
    if visit is None or visit["status"] != "EXPECTED":
        return
    warehouse = conn.execute("select destination_facility_id from shipments where shipment_id=?", (shipment_id,)).fetchone()[0]
    conn.execute("update truck_visits set warehouse_id=?, row_version=row_version+1 where truck_visit_id=?",
                 (warehouse, visit["truck_visit_id"]))
    # A pass that had already run out needs replacing too: a later booking would otherwise revive it for the old warehouse.
    expired = conn.execute(
        "update dock_access_authorizations set status='REVOKED', revoked_at=?, revocation_reason='WAREHOUSE_CHANGED',"
        " row_version=row_version+1 where status='EXPIRED' and rowid=(select max(q.rowid) from dock_access_authorizations q"
        " where q.truck_visit_id=?)", (utc(clock.now()), visit["truck_visit_id"])).rowcount
    if _end_live(conn, visit["truck_visit_id"], "REVOKED", "WAREHOUSE_CHANGED", clock) or expired:
        record(conn, None, "PASS_REVOKED", shipment_id, clock, detail={"reason": "WAREHOUSE_CHANGED"})


def send_pending(conn: sqlite3.Connection, client: TelegramClient | None, clock: Clock) -> int:
    """Send a new pass for each shipment whose pass was revoked by a warehouse move; returns how many reached the driver.

    Nothing is issued while it could not be sent, and a pass whose sending failed is withdrawn and tried again later, so
    the shipment stays pending until the driver has received one. Partner warehouses issue none.
    """
    if client is None:
        return 0
    rows = conn.execute(
        "select s.shipment_id, s.driver_id, t.truck_visit_id, p.revocation_reason, p.revoked_at from shipments s"
        " join facilities f on f.facility_id=s.destination_facility_id"
        " join visit_shipments v using(shipment_id) join truck_visits t using(truck_visit_id)"
        " join dock_access_authorizations p on p.rowid=(select max(q.rowid) from dock_access_authorizations q"
        " where q.truck_visit_id=t.truck_visit_id) where p.status='REVOKED' and p.revocation_reason in"
        " ('WAREHOUSE_CHANGED','SEND_FAILED') and t.status='EXPECTED' and f.is_partner=0 and s.current_status<>'CANCELLED'").fetchall()
    count = 0
    for r in rows:
        if r["revocation_reason"] == "SEND_FAILED" and clock.now() - parse_operational_timestamp(r["revoked_at"]) < RETRY_AFTER:
            continue
        if bound_chat(conn, r["driver_id"]) is None:
            continue
        try:
            sent = send_new(conn, client, actor=None, shipment_id=r["shipment_id"], clock=clock)
        except AuthError:
            continue
        if sent:
            count += 1
            continue
        with immediate(conn):
            conn.execute("update dock_access_authorizations set status='REVOKED', revoked_at=?, revocation_reason='SEND_FAILED',"
                         " row_version=row_version+1 where truck_visit_id=? and status='ISSUED' and delivered_at is null",
                         (utc(clock.now()), r["truck_visit_id"]))
    return count


def revoke(conn: sqlite3.Connection, *, actor: User, shipment_id: str, reason: str, clock: Clock) -> None:
    if not reason.strip():
        raise AuthError("REASON_REQUIRED")
    with immediate(conn):
        _shipment(conn, shipment_id, actor)
        visit = visit_for(conn, shipment_id)
        # Revoking under a truck that is inside would hide it; the visit ends at check-out.
        if visit is not None and visit["status"] != "EXPECTED":
            raise AuthError("VISIT_IN_PROGRESS" if visit["status"] != "CHECKED_OUT" else "VISIT_CLOSED")
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


def driver_language(conn: sqlite3.Connection, driver_id: str) -> str:
    # The language the driver last wrote in, if they have written.
    row = conn.execute("select json_extract(state_json, '$.language') from conversation_state where driver_id=?"
                       " order by updated_at desc limit 1", (driver_id,)).fetchone()
    return str(row[0]) if row and row[0] else "en"


def deliver(conn: sqlite3.Connection, client: TelegramClient | None, shipment_id: str, code: str, clock: Clock) -> bool:
    """Send the code to the driver's linked chat. Call outside a transaction: it talks to the network."""
    ship = conn.execute("select driver_id, order_reference from shipments where shipment_id=?", (shipment_id,)).fetchone()
    chat_id = bound_chat(conn, ship["driver_id"])
    reason = None
    if client is None:
        reason = "NOT_CONFIGURED"
    elif chat_id is None:
        reason = "DRIVER_NOT_LINKED"
    else:
        try:
            client.send(chat_id, say("pass_delivery", driver_language(conn, ship["driver_id"]), order=ship["order_reference"], code=code))
        except TelegramSendError:
            reason = "SEND_FAILED"
    with immediate(conn):
        if reason is None:
            conn.execute("update dock_access_authorizations set delivered_at=? where token_hash=?", (utc(clock.now()), hash_code(code)))
        record(conn, None, "PASS_DELIVERED" if reason is None else "PASS_DELIVERY_FAILED", shipment_id, clock,
               "OK" if reason is None else "FAILED", {"reason": reason} if reason else None)
    return reason is None


def send_new(conn: sqlite3.Connection, client: TelegramClient | None, *, actor: User | None, shipment_id: str, clock: Clock) -> bool:
    """Replace the shipment's pass and send it. When a person asks (lost or compromised), the driver must send it back again."""
    with immediate(conn):
        code = issue(conn, shipment_id, clock, actor)
        if actor is not None:
            conn.execute("update shipment_assignments set confirmed_at=null, failed_attempts=0, locked_at=null where shipment_id=?",
                         (shipment_id,))
    return deliver(conn, client, shipment_id, code, clock)


def send_waiting(conn: sqlite3.Connection, client: TelegramClient | None, driver_id: str, clock: Clock) -> None:
    """A driver has just linked Telegram: send a pass for each shipment still waiting for them."""
    rows = conn.execute("select s.shipment_id from shipments s join shipment_assignments a using(shipment_id)"
                        " where s.driver_id=? and a.confirmed_at is null and s.current_status<>'CANCELLED'", (driver_id,)).fetchall()
    for r in rows:
        try:
            send_new(conn, client, actor=None, shipment_id=r["shipment_id"], clock=clock)
        except AuthError:
            # No appointment or a visit under way: nothing to send for that one.
            continue


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
