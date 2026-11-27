"""Server-side web sessions: a random cookie token, stored only as its sha256."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

# hashlib: only the hash of a token is stored; secrets: unguessable tokens.
import hashlib
import secrets
import sqlite3

# timedelta for the session lifetimes; Literal for the closed method set.
from datetime import timedelta
from typing import Literal

from setuhaul.auth import users
from setuhaul.clock import Clock

# Parses stored timestamps (operational IST format).
from setuhaul.db import parse_operational_timestamp

# The browser cookie that carries the raw token.
COOKIE_NAME = "setuhaul_session"
# Google and password sign-in last a working day; break-glass local admin lasts one hour.
_TTL = {"GOOGLE": timedelta(hours=8), "LOCAL_ADMIN": timedelta(hours=1), "PASSWORD": timedelta(hours=8)}


def _hash(token: str) -> str:
    # The database holds only the hash, so a copy of it cannot be replayed as a cookie.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create(
    conn: sqlite3.Connection, user: users.User, method: Literal["GOOGLE", "LOCAL_ADMIN", "PASSWORD"], clock: Clock
) -> str:
    # 32 random bytes, URL-safe: infeasible to guess.
    token = secrets.token_urlsafe(32)
    # Created and expiry times come from the injected clock.
    now = clock.now()
    expires = now + _TTL[method]
    # Store the hash as the session id, never the token itself.
    conn.execute(
        "insert into auth_sessions(session_id, user_id, created_at, expires_at, revoked_at, auth_method) "
        "values (?,?,?,?,NULL,?)",
        (_hash(token), user.user_id, now.isoformat(), expires.isoformat(), method),
    )
    # Every sign-in path comes through here, so the admin screen's "last login" is recorded with the session.
    conn.execute("update users set last_login_at=? where user_id=?", (now.isoformat(), user.user_id))
    conn.commit()
    # The raw token goes to the caller once, for the cookie.
    return token


def resolve(conn: sqlite3.Connection, token: str, clock: Clock) -> users.User | None:
    # Look up by the hash of the presented token.
    row = conn.execute(
        "select user_id, expires_at, revoked_at from auth_sessions where session_id=?", (_hash(token),)
    ).fetchone()
    # Unknown and revoked tokens are indistinguishable to the caller.
    if row is None or row["revoked_at"] is not None:
        return None
    # Past its expiry: refuse.
    if parse_operational_timestamp(row["expires_at"]) <= clock.now():
        return None
    # Re-read the user every time so a role change or disable applies on the very next request.
    user = users.find_by_id(conn, row["user_id"])
    if user is None or user.status != "ACTIVE":
        return None
    return user


def revoke(conn: sqlite3.Connection, token: str, clock: Clock) -> None:
    # Only a still-live session is stamped, so an earlier revocation time is kept.
    conn.execute(
        "update auth_sessions set revoked_at=? where session_id=? and revoked_at is null",
        (clock.now().isoformat(), _hash(token)),
    )
    conn.commit()


def cookie_kwargs(secure: bool) -> dict[str, object]:
    # HttpOnly keeps scripts away from the token; Lax still allows the OAuth redirect back.
    return {"httponly": True, "samesite": "lax", "secure": secure, "path": "/"}
