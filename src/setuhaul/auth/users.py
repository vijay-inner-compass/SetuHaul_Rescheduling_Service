"""Invite-only web users: invite, look up, disable, and the bootstrap admin."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

# secrets: unguessable short ids for new users.
import secrets

# sqlite3 for the Connection and Row types.
import sqlite3

# Literal for the closed role set.
from typing import Literal

# Frozen validated model, same style as identity.py.
from pydantic import BaseModel, ConfigDict

from setuhaul.auth import audit
from setuhaul.clock import Clock

# The injected clock is used for every timestamp; the db module provides the write lock helper.
from setuhaul.db import immediate

# Reuse the stable-code authorisation error.
from setuhaul.identity import AuthError

Role = Literal["ADMIN", "WAREHOUSE_AUTHORITY", "SECURITY"]
# Roles that act only at the facilities they are bound to.
FACILITY_BOUND = ("WAREHOUSE_AUTHORITY", "SECURITY")


# A web user as the rest of the system may see it.
class User(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    email: str
    role: Role
    status: Literal["ACTIVE", "DISABLED"]
    display_name: str | None
    facilities: tuple[str, ...]


def _normalise(email: str) -> str:
    # Emails are stored trimmed and lower-cased; the column is also NOCASE.
    return email.strip().lower()


def _load(conn: sqlite3.Connection, where: str, param: str) -> User | None:
    # One user row, matched by the caller's (fixed, internal) condition.
    row = conn.execute(
        f"select user_id, email, role, status, display_name from users where deleted_at is null and ({where})", (param,)
    ).fetchone()
    if row is None:
        return None
    # The facilities a warehouse authority manages, in a stable order.
    facs = conn.execute(
        "select facility_id from user_facilities where user_id=? order by facility_id", (row["user_id"],)
    ).fetchall()
    return User(
        user_id=row["user_id"],
        email=row["email"],
        role=row["role"],
        status=row["status"],
        display_name=row["display_name"],
        facilities=tuple(f[0] for f in facs),
    )


def find_by_email(conn: sqlite3.Connection, email: str) -> User | None:
    # Case-insensitive match on the trimmed address.
    return _load(conn, "email = ? collate nocase", _normalise(email))


def find_by_id(conn: sqlite3.Connection, user_id: str) -> User | None:
    # Fresh read by primary key, so callers such as sessions never see a cached role or status.
    return _load(conn, "user_id = ?", user_id)


def _new_id() -> str:
    # Server-generated, never supplied by the caller.
    return "USR-" + secrets.token_hex(5).upper()


def invite(
    conn: sqlite3.Connection,
    *,
    actor: User,
    email: str,
    role: Role,
    display_name: str,
    facilities: tuple[str, ...] = (),
    clock: Clock,
) -> User:
    # Only an active admin may invite; the refusal is audited.
    if actor.role != "ADMIN" or actor.status != "ACTIVE":
        audit.record(
            conn, actor_user_id=actor.user_id, actor_email=actor.email, action="INVITE", target_type="user",
            target_id=None, outcome="FORBIDDEN", ip=None, detail={"role": role}, clock=clock,
        )
        conn.commit()
        raise AuthError("FORBIDDEN")
    addr = _normalise(email)
    # A blank display name is not a name.
    display_name = display_name.strip()
    if not display_name:
        raise AuthError("INVALID_NAME")
    # A syntactically empty address is not an invite.
    if "@" not in addr:
        raise AuthError("INVALID_EMAIL")
    # A facility-bound user needs at least one facility, and each must exist.
    if role in FACILITY_BOUND:
        if not facilities:
            raise AuthError("FACILITY_REQUIRED")
        for fac in facilities:
            if conn.execute("select 1 from facilities where facility_id=?", (fac,)).fetchone() is None:
                raise AuthError("UNKNOWN_FACILITY", fac)
    # One transaction: the user, their facilities and the audit row stand or fall together.
    with immediate(conn):
        # A second invite for the same address is refused rather than overwritten.
        if find_by_email(conn, addr) is not None:
            raise AuthError("ALREADY_INVITED")
        uid = _new_id()
        conn.execute(
            "insert into users(user_id, email, role, status, display_name, invited_by, created_at) "
            "values (?,?,?,'ACTIVE',?,?,?)",
            (uid, addr, role, display_name, actor.user_id, clock.now().isoformat()),
        )
        # Facilities only apply to facility-bound roles.
        if role in FACILITY_BOUND:
            for fac in facilities:
                conn.execute("insert into user_facilities(user_id, facility_id) values (?,?)", (uid, fac))
        audit.record(
            conn, actor_user_id=actor.user_id, actor_email=actor.email, action="INVITE", target_type="user",
            target_id=uid, outcome="OK", ip=None, detail={"role": role, "facilities": list(facilities)}, clock=clock,
        )
    created = find_by_email(conn, addr)
    assert created is not None
    return created


def disable(conn: sqlite3.Connection, *, actor: User, user_id: str, clock: Clock) -> None:
    # Only an active admin may disable (a quick check on the object; the database is re-read below).
    if actor.role != "ADMIN" or actor.status != "ACTIVE":
        raise AuthError("FORBIDDEN")
    # Imported here: that module reads users through this one.
    from setuhaul.auth import links

    now = clock.now().isoformat()
    with immediate(conn):
        # Re-read the actor under the write lock: a stale object must not outlive a disable or role change.
        fresh = _load(conn, "user_id = ?", actor.user_id)
        if fresh is None or fresh.role != "ADMIN" or fresh.status != "ACTIVE":
            raise AuthError("FORBIDDEN")
        # Nobody disables themselves.
        if user_id == actor.user_id:
            raise AuthError("CANNOT_DISABLE_SELF")
        target = _load(conn, "user_id = ?", user_id)
        if target is None:
            raise AuthError("UNKNOWN_USER")
        # Never leave the system without an active admin: some OTHER active admin must remain.
        if target.role == "ADMIN" and target.status == "ACTIVE":
            others = conn.execute("select count(*) from users where role='ADMIN' and status='ACTIVE' and user_id<>?",
                                  (user_id,)).fetchone()[0]
            if others == 0:
                raise AuthError("LAST_ADMIN")
        # Mark the account disabled so no new login succeeds.
        conn.execute("update users set status='DISABLED' where user_id=?", (user_id,))
        # Kill every live session at once; already-revoked ones keep their original time.
        conn.execute(
            "update auth_sessions set revoked_at=? where user_id=? and revoked_at is null", (now, user_id)
        )
        # An emailed link must not outlive the disable, even if the account is enabled again.
        links.cancel_all(conn, user_id, clock)
        audit.record(
            conn, actor_user_id=actor.user_id, actor_email=actor.email, action="DISABLE", target_type="user",
            target_id=user_id, outcome="OK", ip=None, detail=None, clock=clock,
        )


def _fresh_admin(conn: sqlite3.Connection, actor: User) -> None:
    # Called under the write lock: the actor is re-read, so a stale object cannot outlive a disable or role change.
    fresh = _load(conn, "user_id = ?", actor.user_id)
    if fresh is None or fresh.role != "ADMIN" or fresh.status != "ACTIVE":
        raise AuthError("FORBIDDEN")



_HISTORY_REFS: tuple[tuple[str, str], ...] = (
    ("users", "invited_by"),
    ("driver_profiles", "approved_by"),
    ("driver_profiles", "created_by_user_id"),
    ("projects", "created_by"),
    ("project_drivers", "allocated_by"),
    ("shipment_assignments", "created_by"),
    ("dock_blocks", "created_by"),
    ("dock_blocks", "ended_by"),
    ("driver_deletions", "deleted_by"),
    ("driver_deletions", "restored_by"),
)


def delete(conn: sqlite3.Connection, *, actor: User, user_id: str, clock: Clock) -> bool:
    """Delete a web user (e.g. warehouse supervisor). Returns True if hard-deleted, False if soft-deleted to keep FKs."""
    if actor.role != "ADMIN" or actor.status != "ACTIVE":
        raise AuthError("FORBIDDEN")
    from setuhaul.auth import links

    now = clock.now().isoformat()
    with immediate(conn):
        _fresh_admin(conn, actor)
        if user_id == actor.user_id:
            raise AuthError("CANNOT_DELETE_SELF")
        target = _load(conn, "user_id = ?", user_id)
        if target is None:
            raise AuthError("UNKNOWN_USER")
        if target.role == "ADMIN" and target.status == "ACTIVE":
            others = conn.execute(
                "select count(*) from users where role='ADMIN' and status='ACTIVE' and deleted_at is null and user_id<>?",
                (user_id,),
            ).fetchone()[0]
            if others == 0:
                raise AuthError("LAST_ADMIN")
        # End all sessions, password links, credentials, lockouts and facility bindings immediately.
        conn.execute("update auth_sessions set revoked_at=? where user_id=? and revoked_at is null", (now, user_id))
        links.cancel_all(conn, user_id, clock)
        conn.execute("delete from admin_credentials where user_id=?", (user_id,))
        conn.execute("delete from auth_ip_lockouts where user_id=?", (user_id,))
        conn.execute("delete from user_facilities where user_id=?", (user_id,))
        referenced = any(
            conn.execute(f"select 1 from {tbl} where {col}=? limit 1", (user_id,)).fetchone() is not None
            for tbl, col in _HISTORY_REFS
        )
        if not referenced:
            conn.execute("delete from account_links where user_id=?", (user_id,))
            conn.execute("delete from auth_sessions where user_id=?", (user_id,))
            conn.execute("delete from users where user_id=?", (user_id,))
            hard_deleted = True
        else:
            # Soft-delete and free the original email so the address can be re-invited later while keeping FKs valid.
            conn.execute(
                "update users set status='DISABLED', deleted_at=?, email=? where user_id=?",
                (now, f"deleted+{user_id.lower()}@deleted.invalid", user_id),
            )
            hard_deleted = False
        audit.record(
            conn,
            actor_user_id=actor.user_id,
            actor_email=actor.email,
            action="USER_DELETED",
            target_type="user",
            target_id=user_id,
            outcome="OK",
            ip=None,
            detail={"role": target.role, "email": target.email, "hard_deleted": hard_deleted},
            clock=clock,
        )
    return hard_deleted


def enable(conn: sqlite3.Connection, *, actor: User, user_id: str, clock: Clock) -> None:
    """Undo a disable. Sessions that were ended stay ended: the user signs in again."""
    with immediate(conn):
        _fresh_admin(conn, actor)
        if _load(conn, "user_id = ?", user_id) is None:
            raise AuthError("UNKNOWN_USER")
        conn.execute("update users set status='ACTIVE' where user_id=?", (user_id,))
        audit.record(
            conn, actor_user_id=actor.user_id, actor_email=actor.email, action="ENABLE", target_type="user",
            target_id=user_id, outcome="OK", ip=None, detail=None, clock=clock,
        )


def set_facilities(conn: sqlite3.Connection, *, actor: User, user_id: str, facilities: tuple[str, ...], clock: Clock) -> None:
    """Replace the facilities of a facility-bound user; it takes effect on their next request."""
    wanted = tuple(sorted(set(facilities)))
    with immediate(conn):
        _fresh_admin(conn, actor)
        target = _load(conn, "user_id = ?", user_id)
        if target is None:
            raise AuthError("UNKNOWN_USER")
        if target.role not in FACILITY_BOUND:
            raise AuthError("NOT_FACILITY_BOUND")
        if not wanted:
            raise AuthError("FACILITY_REQUIRED")
        for fac in wanted:
            if conn.execute("select 1 from facilities where facility_id=?", (fac,)).fetchone() is None:
                raise AuthError("UNKNOWN_FACILITY", fac)
        conn.execute("delete from user_facilities where user_id=?", (user_id,))
        for fac in wanted:
            conn.execute("insert into user_facilities(user_id, facility_id) values (?,?)", (user_id, fac))
        audit.record(
            conn, actor_user_id=actor.user_id, actor_email=actor.email, action="FACILITIES_CHANGED", target_type="user",
            target_id=user_id, outcome="OK", ip=None, detail={"before": list(target.facilities), "after": list(wanted)}, clock=clock,
        )


def bootstrap_admin(conn: sqlite3.Connection, email: str, clock: Clock) -> User:
    addr = _normalise(email)
    with immediate(conn):
        # Idempotent: an existing user with this address is returned unchanged.
        existing = find_by_email(conn, addr)
        if existing is not None:
            # The owner's way back from a lockout: a disabled or demoted account becomes an active admin again.
            if existing.role != "ADMIN" or existing.status != "ACTIVE":
                conn.execute("update users set role='ADMIN', status='ACTIVE' where user_id=?", (existing.user_id,))
                audit.record(
                    conn, actor_user_id=None, actor_email=None, action="BOOTSTRAP_ADMIN_REACTIVATED", target_type="user",
                    target_id=existing.user_id, outcome="OK", ip=None,
                    detail={"was_role": existing.role, "was_status": existing.status}, clock=clock,
                )
                reactivated = find_by_email(conn, addr)
                assert reactivated is not None
                return reactivated
            return existing
        uid = _new_id()
        conn.execute(
            "insert into users(user_id, email, role, status, display_name, invited_by, created_at) "
            "values (?,?,'ADMIN','ACTIVE',?,NULL,?)",
            (uid, addr, addr, clock.now().isoformat()),
        )
        audit.record(
            conn, actor_user_id=None, actor_email=None, action="BOOTSTRAP_ADMIN", target_type="user",
            target_id=uid, outcome="OK", ip=None, detail=None, clock=clock,
        )
    created = find_by_email(conn, addr)
    assert created is not None
    return created
