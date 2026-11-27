"""One-time links for setting a password: a random token, stored only as its sha256, usable once."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

# hashlib: only the hash of a token is stored; secrets: unguessable tokens.
import hashlib
import secrets
import sqlite3
from datetime import timedelta
from typing import Literal

from setuhaul.auth import audit, local_admin, users
from setuhaul.clock import Clock
from setuhaul.db import immediate, parse_operational_timestamp
from setuhaul.identity import AuthError

Purpose = Literal["INVITE", "RESET"]
# How long each kind of link works.
LIFETIME: dict[Purpose, timedelta] = {"INVITE": timedelta(hours=48), "RESET": timedelta(hours=1)}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def cancel_all(conn: sqlite3.Connection, user_id: str, clock: Clock) -> None:
    """Spend every unused link of a user, inside the caller's transaction."""
    conn.execute("update account_links set used_at=? where user_id=? and used_at is null",
                 (clock.now().isoformat(), user_id))


def issue(conn: sqlite3.Connection, user_id: str, purpose: Purpose, clock: Clock) -> str:
    """A new link token. The user's earlier unused links stop working."""
    token = secrets.token_urlsafe(32)
    now = clock.now()
    with immediate(conn):
        cancel_all(conn, user_id, clock)
        conn.execute(
            "insert into account_links(token_hash, user_id, purpose, created_at, expires_at, used_at) values (?,?,?,?,?,NULL)",
            (_hash(token), user_id, purpose, now.isoformat(), (now + LIFETIME[purpose]).isoformat()),
        )
    return token


def redeem(conn: sqlite3.Connection, token: str, password: str, clock: Clock, ip: str | None = None) -> users.User:
    """Set the password the link was sent for. Raises LINK_INVALID or a password length code."""
    # First: it validates the length (so a refused password leaves the link usable) and keeps scrypt outside the lock.
    stored, salt_hex = local_admin.hash_new_password(password)
    with immediate(conn):
        row = conn.execute("select user_id, expires_at, used_at from account_links where token_hash=?",
                           (_hash(token),)).fetchone()
        # Unknown, used, expired and disabled all read the same.
        if row is None or row["used_at"] is not None or parse_operational_timestamp(row["expires_at"]) <= clock.now():
            raise AuthError("LINK_INVALID")
        user = users.find_by_id(conn, row["user_id"])
        if user is None or user.status != "ACTIVE":
            raise AuthError("LINK_INVALID")
        now = clock.now().isoformat()
        conn.execute("update account_links set used_at=? where token_hash=?", (now, _hash(token)))
        local_admin.store_password(conn, user.user_id, stored, salt_hex)
        # Whoever was signed in with the old password, or without one, has to sign in again.
        conn.execute("update auth_sessions set revoked_at=? where user_id=? and revoked_at is null", (now, user.user_id))
        audit.record(conn, actor_user_id=user.user_id, actor_email=user.email, action="PASSWORD_SET", target_type="user",
                     target_id=user.user_id, outcome="OK", ip=ip, detail=None, clock=clock)
    return user
