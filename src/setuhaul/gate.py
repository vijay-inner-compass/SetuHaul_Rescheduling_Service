"""The gate: check a pass against what the guard sees, then move the visit through check-in, unloading and check-out.

A refusal says only why; nothing about the shipment is returned until every check passes. Each change is one
immediate transaction, so two people pressing at once cannot both succeed.
"""

# Lazy annotations.
from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from redis import Redis

from setuhaul import passes
from setuhaul.auth.authz import facility_clause, facility_ids
from setuhaul.auth.onboarding import driver_eligible
from setuhaul.auth.users import User
from setuhaul.clock import Clock
from setuhaul.db import IST, immediate, parse_operational_timestamp
from setuhaul.dock_pass import hash_code
from setuhaul.escalation import HandoffRequest, create_handoff
from setuhaul.events import append_appointment_event
from setuhaul.identity import AuthError

# Failed checks by one user before they must wait, and for how long.
MAX_FAILURES = 10
FAILURE_WINDOW_S = 900
GATE_ROLES = ("SECURITY", "ADMIN")


class GateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=80)
    vehicle_registration: str = Field(min_length=1, max_length=40)
    licence_number: str = Field(min_length=1, max_length=60)
    # Made by the screen for one truck, so the same press repeated is not a second action.
    request_key: str = Field(min_length=1, max_length=64)


class GateView(BaseModel):
    model_config = ConfigDict(frozen=True)

    truck_visit_id: str
    driver_name: str
    order_reference: str
    vehicle_registration: str
    dock_code: str | None
    valid_until: str


def _same(a: str, b: str) -> bool:
    # Case, spaces and hyphens are formatting.
    return re.sub(r"[\s-]", "", a).upper() == re.sub(r"[\s-]", "", b).upper()


def _guard(actor: User, roles: tuple[str, ...]) -> None:
    if actor.status != "ACTIVE" or actor.role not in roles:
        raise AuthError("FORBIDDEN")


def _in_scope(actor: User, warehouse_id: str) -> bool:
    scope = facility_ids(actor)
    return scope is None or warehouse_id in scope


def _pass(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    row = conn.execute(
        "select p.*, t.status as visit_status, t.check_in_key, v.shipment_id from dock_access_authorizations p"
        " join truck_visits t using(truck_visit_id) join visit_shipments v using(truck_visit_id) where p.token_hash=?",
        (hash_code(code),)).fetchone()
    return row  # type: ignore[no-any-return]


def _view(conn: sqlite3.Connection, row: sqlite3.Row) -> GateView:
    d = conn.execute("select d.driver_name, s.order_reference, k.dock_code from shipments s join drivers d on d.driver_id=s.driver_id"
                     " left join docks k on k.dock_id=? where s.shipment_id=?", (row["dock_id"], row["shipment_id"])).fetchone()
    until = parse_operational_timestamp(row["valid_until_utc"]).astimezone(IST).isoformat()
    return GateView(truck_visit_id=row["truck_visit_id"], driver_name=d["driver_name"], order_reference=d["order_reference"],
                    vehicle_registration=row["vehicle_registration"], dock_code=d["dock_code"], valid_until=until)


def _assess(conn: sqlite3.Connection, actor: User, data: GateRequest, clock: Clock) -> sqlite3.Row:
    """The pass, if every check passes; otherwise the first reason it does not, in a fixed order."""
    row = _pass(conn, data.code)
    if row is None or row["status"] in ("REVOKED", "CANCELLED", "CLOSED", "COMPLETED"):
        raise AuthError("PASS_NOT_VALID")
    if not _in_scope(actor, row["warehouse_id"]):
        raise AuthError("WRONG_WAREHOUSE")
    if row["status"] == "ACTIVE" or row["visit_status"] != "EXPECTED":
        raise AuthError("ALREADY_CHECKED_IN")
    now = clock.now()
    if now < parse_operational_timestamp(row["valid_from_utc"]):
        raise AuthError("TOO_EARLY")
    if row["status"] == "EXPIRED" or now > parse_operational_timestamp(row["valid_until_utc"]):
        raise AuthError("TOO_LATE")
    if passes.current_appointment(conn, row["shipment_id"]) is None:
        raise AuthError("NO_APPOINTMENT")
    if not driver_eligible(conn, row["driver_id"], now.astimezone(IST).date()):
        raise AuthError("DRIVER_NOT_ELIGIBLE")
    licence = conn.execute("select licence_number from drivers where driver_id=?", (row["driver_id"],)).fetchone()[0]
    if not (_same(data.vehicle_registration, row["vehicle_registration"]) and _same(data.licence_number, licence)):
        raise AuthError("DETAILS_DO_NOT_MATCH")
    return row


def _failures_key(actor: User) -> str:
    return f"setuhaul:gate:{actor.user_id}"


def _limit(redis: Redis, actor: User) -> None:
    if int(redis.get(_failures_key(actor)) or 0) >= MAX_FAILURES:
        raise AuthError("TOO_MANY_ATTEMPTS")


def _audit(conn: sqlite3.Connection, actor: User, action: str, target: str | None, clock: Clock, outcome: str = "OK",
           reason: str | None = None) -> None:
    passes.record(conn, actor, action, target or "", clock, outcome, {"reason": reason} if reason else None)


def _refused(conn: sqlite3.Connection, redis: Redis, actor: User, data: GateRequest, reason: str, clock: Clock) -> None:
    """Count and record a failed check. Called after its transaction has rolled back."""
    if redis.incr(_failures_key(actor)) == 1:
        redis.expire(_failures_key(actor), FAILURE_WINDOW_S)
    row = _pass(conn, data.code)
    with immediate(conn):
        _audit(conn, actor, "GATE_CHECK", row["shipment_id"] if row else None, clock, "REFUSED", reason)


def validate(conn: sqlite3.Connection, redis: Redis, *, actor: User, data: GateRequest, clock: Clock) -> GateView:
    _guard(actor, GATE_ROLES)
    _limit(redis, actor)
    try:
        with immediate(conn):
            row = _assess(conn, actor, data, clock)
            conn.execute("update dock_access_authorizations set status='VALIDATED', row_version=row_version+1"
                         " where authorization_id=? and status='ISSUED'", (row["authorization_id"],))
            _audit(conn, actor, "GATE_CHECK", row["shipment_id"], clock)
            return _view(conn, row)
    except AuthError as error:
        _refused(conn, redis, actor, data, error.code, clock)
        raise


def check_in(conn: sqlite3.Connection, redis: Redis, *, actor: User, data: GateRequest, clock: Clock) -> GateView:
    _guard(actor, GATE_ROLES)
    _limit(redis, actor)
    try:
        with immediate(conn):
            done = _pass(conn, data.code)
            # The same press again, after it worked.
            if (done is not None and done["status"] == "ACTIVE" and done["check_in_key"] == data.request_key
                    and _in_scope(actor, done["warehouse_id"])):
                return _view(conn, done)
            row = _assess(conn, actor, data, clock)
            _record_arrival(conn, row, data.request_key, clock)
            _audit(conn, actor, "CHECK_IN", row["shipment_id"], clock)
            return _view(conn, row)
    except AuthError as error:
        _refused(conn, redis, actor, data, error.code, clock)
        raise


def _record_arrival(conn: sqlite3.Connection, row: sqlite3.Row, request_key: str, clock: Clock) -> None:
    now, shipment_id = clock.now().astimezone(IST), row["shipment_id"]
    stamp = now.isoformat()
    changed = conn.execute(
        "update dock_access_authorizations set status='ACTIVE', activated_at=?, row_version=row_version+1"
        " where authorization_id=? and status in ('ISSUED','VALIDATED') and row_version=?",
        (passes.utc(now), row["authorization_id"], row["row_version"])).rowcount
    if changed != 1:
        raise AuthError("ALREADY_CHECKED_IN")
    conn.execute("update truck_visits set status='CHECKED_IN', checked_in_at=?, check_in_key=?, row_version=row_version+1"
                 " where truck_visit_id=?", (stamp, request_key, row["truck_visit_id"]))
    conn.execute("update shipments set current_status='AT_GATE', updated_at=? where shipment_id=?", (stamp, shipment_id))
    # The pass was shown in person, which is at least as good as sending it back in chat.
    conn.execute("update shipment_assignments set confirmed_at=? where shipment_id=? and confirmed_at is null", (stamp, shipment_id))
    appointment = passes.current_appointment(conn, shipment_id)
    assert appointment is not None
    start = parse_operational_timestamp(appointment["slot_start_ts"])
    grace = timedelta(minutes=conn.execute("select checkin_grace_min from facilities where facility_id=?",
                                           (row["warehouse_id"],)).fetchone()[0])
    arrival = "LATE" if now > start + grace else "EARLY" if now < start - grace else "ON_TIME"
    conn.execute("insert into facility_checkins(checkin_id, shipment_id, facility_id, gate_in_ts, arrival_state, updated_at)"
                 " values (?,?,?,?,?,?) on conflict(shipment_id) do update set facility_id=excluded.facility_id,"
                 " gate_in_ts=excluded.gate_in_ts, arrival_state=excluded.arrival_state, updated_at=excluded.updated_at",
                 ("CHK-" + uuid.uuid4().hex[:10].upper(), shipment_id, row["warehouse_id"], stamp, arrival, stamp))


def _visit(conn: sqlite3.Connection, actor: User, truck_visit_id: str | None) -> sqlite3.Row:
    row = conn.execute("select t.*, v.shipment_id from truck_visits t join visit_shipments v using(truck_visit_id)"
                       " where t.truck_visit_id=?", (truck_visit_id,)).fetchone()
    if row is None or not _in_scope(actor, row["warehouse_id"]):
        raise AuthError("VISIT_NOT_FOUND")
    return row  # type: ignore[no-any-return]


def unloading_complete(conn: sqlite3.Connection, *, actor: User, truck_visit_id: str, clock: Clock) -> None:
    """The warehouse says the truck is unloaded: the dock is free and the truck may leave."""
    _guard(actor, ("WAREHOUSE_AUTHORITY", "ADMIN"))
    with immediate(conn):
        visit = _visit(conn, actor, truck_visit_id)
        if visit["status"] == "OPERATIONS_COMPLETE":
            return
        if visit["status"] != "CHECKED_IN":
            raise AuthError("NOT_CHECKED_IN")
        now = clock.now()
        stamp = now.astimezone(IST).isoformat()
        conn.execute("update dock_access_authorizations set status='COMPLETED', row_version=row_version+1"
                     " where truck_visit_id=? and status='ACTIVE'", (truck_visit_id,))
        conn.execute("update truck_visits set status='OPERATIONS_COMPLETE', completed_at=?, row_version=row_version+1"
                     " where truck_visit_id=?", (stamp, truck_visit_id))
        appointment = passes.current_appointment(conn, visit["shipment_id"])
        if appointment is not None:
            before = conn.execute("select appointment_status from appointments where appointment_id=?",
                                  (appointment["appointment_id"],)).fetchone()[0]
            conn.execute("update appointments set appointment_status='COMPLETED', updated_at=? where appointment_id=?",
                         (stamp, appointment["appointment_id"]))
            append_appointment_event(conn, appointment["appointment_id"], visit["shipment_id"], "UNLOADING_COMPLETE",
                                     from_status=before, to_status="COMPLETED", actor_type="WAREHOUSE", actor_id=actor.user_id,
                                     reason="UNLOADING_COMPLETE", occurred_at=now)
        _audit(conn, actor, "UNLOADING_COMPLETE", visit["shipment_id"], clock)


def check_out(conn: sqlite3.Connection, *, actor: User, truck_visit_id: str | None, code: str | None, request_key: str,
              clock: Clock) -> None:
    """By visit (from the list of trucks inside) or by the pass itself."""
    _guard(actor, GATE_ROLES)
    with immediate(conn):
        if truck_visit_id is None:
            found = _pass(conn, code or "")
            truck_visit_id = found["truck_visit_id"] if found else None
        visit = _visit(conn, actor, truck_visit_id)
        if visit["status"] == "CHECKED_OUT" and visit["check_out_key"] == request_key:
            return
        if visit["status"] == "CHECKED_IN":
            raise AuthError("UNLOADING_NOT_COMPLETE")
        if visit["status"] != "OPERATIONS_COMPLETE":
            raise AuthError("NOT_CHECKED_IN")
        stamp = clock.now().astimezone(IST).isoformat()
        conn.execute("update dock_access_authorizations set status='CLOSED', row_version=row_version+1"
                     " where truck_visit_id=? and status='COMPLETED'", (visit["truck_visit_id"],))
        conn.execute("update truck_visits set status='CHECKED_OUT', checked_out_at=?, check_out_key=?, row_version=row_version+1"
                     " where truck_visit_id=?", (stamp, request_key, visit["truck_visit_id"]))
        conn.execute("update shipments set current_status='COMPLETED', updated_at=? where shipment_id=?", (stamp, visit["shipment_id"]))
        conn.execute("update facility_checkins set gate_out_ts=?, updated_at=? where shipment_id=?", (stamp, stamp, visit["shipment_id"]))
        _audit(conn, actor, "CHECK_OUT", visit["shipment_id"], clock)


# A truck inside, with the end of its pass window.
_INSIDE = ("select t.truck_visit_id, t.status, t.checked_in_at, t.driver_id, t.warehouse_id, v.shipment_id, d.driver_name,"
           " s.order_reference, p.vehicle_registration, p.valid_until_utc from truck_visits t join visit_shipments v using(truck_visit_id)"
           " join shipments s using(shipment_id) join drivers d on d.driver_id=t.driver_id join dock_access_authorizations p"
           " on p.truck_visit_id=t.truck_visit_id and p.status in ('ACTIVE','COMPLETED')"
           " where t.status in ('CHECKED_IN','OPERATIONS_COMPLETE')")


def inside(conn: sqlite3.Connection, actor: User, clock: Clock | None = None) -> list[dict[str, Any]]:
    """Trucks inside the user's warehouses, longest there first."""
    clause, params = facility_clause(actor, "t.warehouse_id")
    rows = conn.execute(f"{_INSIDE} and {clause} order by t.checked_in_at", params).fetchall()
    out = []
    for r in rows:
        # Overstayed if the sweep has said so, or the window has passed by the caller's clock.
        late = conn.execute("select 1 from audit_log where action='VISIT_OVERSTAY' and target_id=?", (r["shipment_id"],)).fetchone()
        over = late is not None or (clock is not None and clock.now() > parse_operational_timestamp(r["valid_until_utc"]))
        out.append({k: r[k] for k in ("truck_visit_id", "status", "checked_in_at", "driver_name", "order_reference",
                                      "vehicle_registration")} | {"overstayed": over})
    return out


def sweep(conn: sqlite3.Connection, clock: Clock) -> int:
    """Expire unused passes, and ask the warehouse to look at each truck still inside after its window. Releases nothing."""
    with immediate(conn):
        passes.expire_due(conn, clock)
    count = 0
    for r in conn.execute(_INSIDE).fetchall():
        if clock.now() <= parse_operational_timestamp(r["valid_until_utc"]):
            continue
        if conn.execute("select 1 from audit_log where action='VISIT_OVERSTAY' and target_id=?", (r["shipment_id"],)).fetchone():
            continue
        thread = conn.execute("select thread_id from chat_threads where driver_id=? order by opened_at desc limit 1",
                              (r["driver_id"],)).fetchone()
        thread_id = thread[0] if thread else "THR-" + uuid.uuid4().hex[:10].upper()
        if thread is None:
            with immediate(conn):
                conn.execute("insert into chat_threads(thread_id, driver_id, shipment_id, opened_at, thread_status, thread_intent)"
                             " values (?,?,?,?,'OPEN','UNKNOWN')", (thread_id, r["driver_id"], r["shipment_id"], clock.now().isoformat()))
        create_handoff(conn, HandoffRequest(
            reason_code="VISIT_OVERSTAY", driver_id=r["driver_id"], thread_id=thread_id, shipment_id=r["shipment_id"],
            summary=f"Truck {r['vehicle_registration']} is still inside after its pass window.", created_at=clock.now()), clock)
        with immediate(conn):
            passes.record(conn, None, "VISIT_OVERSTAY", r["shipment_id"], clock, detail={"truck_visit_id": r["truck_visit_id"]})
        count += 1
    return count
