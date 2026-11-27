"""Deleting and restoring a driver.

Nothing is erased: the driver is made inactive and hidden, their unfinished loads are cancelled and their slots freed.
"""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import sqlite3

from pydantic import BaseModel, ConfigDict

from setuhaul import passes
from setuhaul.auth import audit
from setuhaul.auth.authz import facility_ids
from setuhaul.auth.onboarding import is_deleted
from setuhaul.auth.telegram import may_manage_binding
from setuhaul.auth.users import User
from setuhaul.booking import release_hold_in_txn
from setuhaul.clock import Clock
from setuhaul.db import IST, immediate, parse_operational_timestamp
from setuhaul.identity import AuthError
from setuhaul.release import FREEABLE, booking, free_in_txn, truck_inside


class Doomed(BaseModel):
    """One unfinished load that deleting the driver would cancel."""

    model_config = ConfigDict(frozen=True)

    shipment_id: str
    order_reference: str
    dock_code: str | None
    start: str | None
    # Why this load stops the whole deletion for now.
    blocked_by: str | None


class Preview(BaseModel):
    model_config = ConfigDict(frozen=True)

    driver_id: str
    driver_name: str
    shipments: list[Doomed]


def preview(conn: sqlite3.Connection, *, actor: User, driver_id: str) -> Preview:
    """What deleting this driver would cancel. Writes nothing."""
    if not may_manage_binding(conn, actor, driver_id):
        raise AuthError("DRIVER_NOT_FOUND")
    name = conn.execute("select driver_name from drivers where driver_id=?", (driver_id,)).fetchone()["driver_name"]
    scope = facility_ids(actor)
    rows = conn.execute(
        "select s.shipment_id, s.order_reference, s.destination_facility_id, a.appointment_status, k.dock_code, sl.slot_start_ts"
        " from shipments s left join appointments a on a.shipment_id=s.shipment_id and a.is_current=1 and a.appointment_status"
        " in ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS') left join appointment_slots sl on sl.slot_id=a.slot_id"
        " left join docks k on k.dock_id=sl.dock_id where s.driver_id=? and s.current_status not in ('COMPLETED','CANCELLED')"
        " order by s.shipment_id", (driver_id,)).fetchall()
    if scope is not None and any(r["destination_facility_id"] not in scope for r in rows):
        raise AuthError("OTHER_FACILITY_SHIPMENTS")
    shipments = []
    for r in rows:
        blocked = "UNLOADING" if r["appointment_status"] == "IN_PROGRESS" else (
            "TRUCK_ON_SITE" if truck_inside(conn, r["shipment_id"]) else None)
        start = r["slot_start_ts"]
        shipments.append(Doomed(
            shipment_id=r["shipment_id"], order_reference=r["order_reference"], dock_code=r["dock_code"],
            start=parse_operational_timestamp(start).astimezone(IST).isoformat() if start else None, blocked_by=blocked))
    return Preview(driver_id=driver_id, driver_name=name, shipments=shipments)


def _cancel_shipment(conn: sqlite3.Connection, actor: User, shipment_id: str, clock: Clock) -> None:
    now = clock.now()
    stamp = now.isoformat()
    marks = ",".join("?" for _ in FREEABLE)
    for r in conn.execute(f"select appointment_id from appointments where shipment_id=? and appointment_status in ({marks})",
                          (shipment_id, *FREEABLE)).fetchall():
        found = booking(conn, r["appointment_id"])
        assert found is not None
        free_in_txn(conn, found, "DRIVER_DELETED", actor, clock)
    # A load with only a held slot has nothing above to release it.
    for r in conn.execute("select appointment_id from appointments where shipment_id=? and appointment_status='HELD'",
                          (shipment_id,)).fetchall():
        release_hold_in_txn(conn, r["appointment_id"], shipment_id, "SYSTEM", now, "WAREHOUSE", actor.user_id, "DRIVER_DELETED")
    passes.cancel(conn, shipment_id, clock)
    conn.execute("update shipments set current_status='CANCELLED', updated_at=? where shipment_id=?", (stamp, shipment_id))
    for table, waiting in (("partner_requests", "PENDING_PARTNER"), ("facility_change_requests", "PENDING")):
        conn.execute(f"update {table} set status='EXPIRED', decided_at=? where shipment_id=? and status=?",
                     (stamp, shipment_id, waiting))


def delete(conn: sqlite3.Connection, *, actor: User, driver_id: str, confirm_name: str, expected: list[str],
           clock: Clock) -> Preview:
    """Delete the driver as the warning showed, all of it or nothing. The driver is sent no message."""
    with immediate(conn):
        plan = preview(conn, actor=actor, driver_id=driver_id)
        if confirm_name.strip().casefold() != plan.driver_name.strip().casefold():
            raise AuthError("NAME_MISMATCH")
        if any(s.blocked_by for s in plan.shipments):
            raise AuthError("DRIVER_ON_SITE")
        if sorted(expected) != sorted(s.shipment_id for s in plan.shipments):
            raise AuthError("PLAN_CHANGED")
        stamp = clock.now().isoformat()
        for s in plan.shipments:
            _cancel_shipment(conn, actor, s.shipment_id, clock)
        previous = conn.execute("select driver_status from drivers where driver_id=?", (driver_id,)).fetchone()[0]
        conn.execute("update drivers set driver_status='INACTIVE' where driver_id=?", (driver_id,))
        conn.execute("update telegram_identities set revoked_at=? where driver_id=? and revoked_at is null", (stamp, driver_id))
        conn.execute("update project_drivers set released_at=? where driver_id=? and released_at is null", (stamp, driver_id))
        conn.execute("insert into driver_deletions(driver_id, deleted_by, deleted_at, previous_status) values (?,?,?,?)",
                     (driver_id, actor.user_id, stamp, previous))
        audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action="DRIVER_DELETED", target_type="driver",
                     target_id=driver_id, outcome="OK", ip=None,
                     detail={"shipments": [s.shipment_id for s in plan.shipments]}, clock=clock)
    return plan


def restore(conn: sqlite3.Connection, *, actor: User, driver_id: str, clock: Clock) -> None:
    """Bring a deleted driver back as they were. Their cancelled loads and project allocations stay as they are."""
    if actor.role != "ADMIN" or actor.status != "ACTIVE":
        raise AuthError("FORBIDDEN")
    with immediate(conn):
        if not is_deleted(conn, driver_id):
            raise AuthError("NOT_DELETED")
        previous = conn.execute("select previous_status from driver_deletions where driver_id=? and restored_at is null",
                                (driver_id,)).fetchone()[0]
        conn.execute("update drivers set driver_status=? where driver_id=?", (previous, driver_id))
        conn.execute("update driver_deletions set restored_by=?, restored_at=? where driver_id=? and restored_at is null",
                     (actor.user_id, clock.now().isoformat(), driver_id))
        audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action="DRIVER_RESTORED", target_type="driver",
                     target_id=driver_id, outcome="OK", ip=None, detail=None, clock=clock)


def deleted(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    # Drivers deleted and not restored, most recent first.
    return list(conn.execute(
        "select d.driver_id, d.driver_name, d.carrier_id, x.deleted_at, u.email as deleted_by from driver_deletions x"
        " join drivers d using(driver_id) join users u on u.user_id=x.deleted_by where x.restored_at is null"
        " order by x.deleted_at desc, d.driver_id"))
