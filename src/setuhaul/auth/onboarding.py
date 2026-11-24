"""Driver onboarding: a warehouse authority adds a driver, an admin approves, and licence expiry is enforced.

Supplied drivers (those without an onboarding profile created by a user) are never updated.
"""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta

from setuhaul.auth import audit
from setuhaul.auth.phone import normalise_e164
from setuhaul.auth.users import User
from setuhaul.clock import Clock
from setuhaul.db import immediate
from setuhaul.identity import ALLOWED_DRIVER_STATUSES, AuthError

# Driver ids look like DRV001; the next one is the highest number plus one.
_DRIVER_ID = re.compile(r"DRV(\d+)")


# A licence valid for more than about 20 years from today is a typo.
_MAX_LICENCE_DAYS = 7305


def _licence_key(number: str) -> str:
    # Case, spaces and hyphens are formatting, not identity.
    return re.sub(r"[\s-]", "", number).upper()


def _refuse_forbidden(conn: sqlite3.Connection, actor: User, action: str, driver_id: str | None, clock: Clock) -> None:
    # An audited refusal, committed before the error leaves.
    audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action=action, target_type="driver",
                 target_id=driver_id, outcome="FORBIDDEN", ip=None, detail={"role": actor.role}, clock=clock)
    conn.commit()
    raise AuthError("FORBIDDEN")


def _next_driver_id(conn: sqlite3.Connection) -> str:
    # Highest numeric suffix among ids shaped DRV<digits>; other ids (GEN-*) are ignored.
    top = 0
    for (existing,) in conn.execute("select driver_id from drivers"):
        m = _DRIVER_ID.fullmatch(existing)
        if m:
            top = max(top, int(m.group(1)))
    return f"DRV{top + 1:03d}"


def add_driver(
    conn: sqlite3.Connection,
    *,
    actor: User,
    name: str,
    phone: str,
    licence_number: str,
    licence_expiry: date,
    carrier_id: str,
    vehicle_id: str,
    clock: Clock,
) -> str:
    # Only active admins and warehouse authorities may add drivers.
    if actor.status != "ACTIVE" or actor.role not in ("ADMIN", "WAREHOUSE_AUTHORITY"):
        _refuse_forbidden(conn, actor, "DRIVER_ADDED", None, clock)
    # Phone must be a real number; it is stored in E.164 form.
    canonical = normalise_e164(phone)
    if canonical is None:
        raise AuthError("INVALID_PHONE")
    licence = licence_number.strip()
    if not licence:
        raise AuthError("INVALID_REQUEST")
    # A sane name: non-empty, bounded, no control characters.
    if not name.strip() or len(name) > 120 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise AuthError("INVALID_NAME")
    # A licence that is already expired is never onboarded, and one decades away is a typo.
    today = clock.now().date()
    if licence_expiry < today:
        raise AuthError("LICENCE_EXPIRED")
    if licence_expiry > today + timedelta(days=_MAX_LICENCE_DAYS):
        raise AuthError("INVALID_LICENCE_EXPIRY")
    # One transaction: the id is chosen, the rows written and the audit row appended together.
    with immediate(conn):
        # The vehicle must exist, be active and belong to the stated carrier.
        veh = conn.execute("select carrier_id, active_flag from vehicles where vehicle_id=?", (vehicle_id,)).fetchone()
        if veh is None:
            raise AuthError("VEHICLE_NOT_FOUND")
        if veh["carrier_id"] != carrier_id:
            raise AuthError("VEHICLE_CARRIER_MISMATCH")
        if not veh["active_flag"]:
            raise AuthError("VEHICLE_INACTIVE")
        # Compare normalised phones against every existing row: supplied numbers are stored hyphenated.
        for (existing,) in conn.execute("select phone from drivers"):
            if normalise_e164(existing) == canonical:
                raise AuthError("DUPLICATE_PHONE")
        # Licence numbers compare ignoring case, spaces and hyphens.
        key = _licence_key(licence)
        for (existing,) in conn.execute("select licence_number from drivers"):
            if _licence_key(existing) == key:
                raise AuthError("DUPLICATE_LICENCE")
        driver_id = _next_driver_id(conn)
        # Inactive until an admin approves.
        conn.execute(
            "insert into drivers(driver_id, carrier_id, driver_name, phone, licence_number, driver_status)"
            " values (?,?,?,?,?,'INACTIVE')", (driver_id, carrier_id, name.strip(), canonical, licence))
        conn.execute(
            "insert into driver_profiles(driver_id, licence_expiry, vehicle_id, approval_status, created_by_user_id)"
            " values (?,?,?,'PENDING_APPROVAL',?)", (driver_id, licence_expiry.isoformat(), vehicle_id, actor.user_id))
        # No phone or licence number in the trail.
        audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action="DRIVER_ADDED",
                     target_type="driver", target_id=driver_id, outcome="OK", ip=None,
                     detail={"carrier_id": carrier_id, "vehicle_id": vehicle_id}, clock=clock)
    return driver_id


def _decide(conn: sqlite3.Connection, actor: User, driver_id: str, clock: Clock, approve: bool) -> None:
    action = "DRIVER_APPROVED" if approve else "DRIVER_REJECTED"
    # Admins only.
    if actor.role != "ADMIN" or actor.status != "ACTIVE":
        _refuse_forbidden(conn, actor, action, driver_id, clock)
    with immediate(conn):
        if conn.execute("select 1 from drivers where driver_id=?", (driver_id,)).fetchone() is None:
            raise AuthError("DRIVER_NOT_FOUND")
        # Only app-created, still-pending profiles can be decided; supplied drivers have none.
        prof = conn.execute(
            "select licence_expiry from driver_profiles"
            " where driver_id=? and created_by_user_id is not null and approval_status='PENDING_APPROVAL'",
            (driver_id,)).fetchone()
        if prof is None:
            raise AuthError("NOT_PENDING")
        now = clock.now()
        if approve:
            # An expired licence cannot be approved.
            if date.fromisoformat(prof["licence_expiry"]) < now.date():
                raise AuthError("LICENCE_EXPIRED")
            conn.execute("update drivers set driver_status='ACTIVE' where driver_id=?", (driver_id,))
        status = "APPROVED" if approve else "REJECTED"
        # On rejection approved_by/approved_at hold the deciding admin and time (schema reuse).
        conn.execute("update driver_profiles set approval_status=?, approved_by=?, approved_at=? where driver_id=?",
                     (status, actor.user_id, now.isoformat(), driver_id))
        audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action=action, target_type="driver",
                     target_id=driver_id, outcome="OK", ip=None, detail=None, clock=clock)


def approve(conn: sqlite3.Connection, *, actor: User, driver_id: str, clock: Clock) -> None:
    # Admin approval activates the driver.
    _decide(conn, actor, driver_id, clock, True)


def reject(conn: sqlite3.Connection, *, actor: User, driver_id: str, clock: Clock) -> None:
    # Rejection leaves the driver inactive.
    _decide(conn, actor, driver_id, clock, False)


def pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    # Drivers waiting for an admin, oldest id first.
    return list(conn.execute(
        "select d.driver_id, d.driver_name, d.phone, d.licence_number, d.carrier_id, p.vehicle_id, p.licence_expiry,"
        " p.created_by_user_id from drivers d join driver_profiles p on p.driver_id=d.driver_id"
        " where p.approval_status='PENDING_APPROVAL' and p.created_by_user_id is not null order by d.driver_id"))


def licence_valid(conn: sqlite3.Connection, driver_id: str, today: date) -> bool:
    """True if the driver may work today.

    A driver with no onboarding profile (the supplied DRV001-DRV015) predates onboarding and is treated as valid.
    An onboarded driver is valid only when APPROVED and the licence has not expired; expiry needs no admin action.
    """
    prof = conn.execute("select licence_expiry, approval_status from driver_profiles where driver_id=?",
                        (driver_id,)).fetchone()
    if prof is None:
        return True
    return bool(prof["approval_status"] == "APPROVED" and date.fromisoformat(prof["licence_expiry"]) >= today)


def driver_eligible(conn: sqlite3.Connection, driver_id: str, today: date) -> bool:
    """True if the driver exists, may work, and (when onboarded through the app) is approved with a valid licence."""
    row = conn.execute("select driver_status from drivers where driver_id=?", (driver_id,)).fetchone()
    if row is None or row["driver_status"] not in ALLOWED_DRIVER_STATUSES:
        return False
    # A supplied driver with no profile predates onboarding; licence_valid treats that as valid, and approval applies only to profiles.
    prof = conn.execute("select approval_status from driver_profiles where driver_id=?", (driver_id,)).fetchone()
    if prof is None:
        return True
    return bool(prof["approval_status"] == "APPROVED" and licence_valid(conn, driver_id, today))
