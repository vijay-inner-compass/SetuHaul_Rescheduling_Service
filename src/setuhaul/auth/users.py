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

Role = Literal["ADMIN", "WAREHOUSE_AUTHORITY"]


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
        f"select user_id, email, role, status, display_name from users where {where}", (param,)
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
    # A syntactically empty address is not an invite.
    if "@" not in addr:
        raise AuthError("INVALID_EMAIL")
    # A warehouse authority must manage at least one facility, and each must exist.
    if role == "WAREHOUSE_AUTHORITY":
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
        # Facilities only apply to warehouse authorities.
        if role == "WAREHOUSE_AUTHORITY":
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
    # Only an active admin may disable.
    if actor.role != "ADMIN" or actor.status != "ACTIVE":
        raise AuthError("FORBIDDEN")
    now = clock.now().isoformat()
    with immediate(conn):
        target = _load(conn, "user_id = ?", user_id)
        if target is None:
            raise AuthError("UNKNOWN_USER")
        # Mark the account disabled so no new login succeeds.
        conn.execute("update users set status='DISABLED' where user_id=?", (user_id,))
        # Kill every live session at once; already-revoked ones keep their original time.
        conn.execute(
            "update auth_sessions set revoked_at=? where user_id=? and revoked_at is null", (now, user_id)
        )
        audit.record(
            conn, actor_user_id=actor.user_id, actor_email=actor.email, action="DISABLE", target_type="user",
            target_id=user_id, outcome="OK", ip=None, detail=None, clock=clock,
        )


def bootstrap_admin(conn: sqlite3.Connection, email: str, clock: Clock) -> User:
    addr = _normalise(email)
    with immediate(conn):
        # Idempotent: an existing user with this address is returned unchanged.
        existing = find_by_email(conn, addr)
        if existing is not None:
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
