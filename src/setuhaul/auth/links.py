"""One-time links for setting a password: a random token, stored only as its sha256, usable once."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

# hashlib: only the hash of a token is stored; secrets: unguessable tokens.
import hashlib
import logging
import secrets
import sqlite3
from datetime import timedelta
from typing import TYPE_CHECKING, Literal

from redis import Redis
from redis.exceptions import RedisError

from setuhaul.auth import account_mail, audit, local_admin, users
from setuhaul.auth.routes import MIN_SECRET_BYTES
from setuhaul.clock import Clock
from setuhaul.db import immediate, open_connection, parse_operational_timestamp
from setuhaul.identity import AuthError

if TYPE_CHECKING:
    from setuhaul.api import AppDeps

Purpose = Literal["INVITE", "RESET"]
Sent = Literal["SENT", "FAILED", "THROTTLED"]
# How long each kind of link works.
LIFETIME: dict[Purpose, timedelta] = {"INVITE": timedelta(hours=48), "RESET": timedelta(hours=1)}
# Sending limits: one per account per cool-down, so many per account per hour, and a cap on all forgot-password mail.
COOL_DOWN_S = 120
PER_ACCOUNT_PER_HOUR = 5
FORGOT_PER_HOUR = 30

_log = logging.getLogger(__name__)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def cancel_all(conn: sqlite3.Connection, user_id: str, clock: Clock) -> None:
    """Spend every unused link of a user, inside the caller's transaction."""
    conn.execute("update account_links set used_at=? where user_id=? and used_at is null",
                 (clock.now().isoformat(), user_id))


def issue(conn: sqlite3.Connection, user_id: str, purpose: Purpose, clock: Clock, *, replace: bool = True) -> str:
    """A new link token. The user's earlier unused links stop working, at once unless ``replace`` is off."""
    token = secrets.token_urlsafe(32)
    now = clock.now()
    with immediate(conn):
        if replace:
            cancel_all(conn, user_id, clock)
        conn.execute(
            "insert into account_links(token_hash, user_id, purpose, created_at, expires_at, used_at) values (?,?,?,?,?,NULL)",
            (_hash(token), user_id, purpose, now.isoformat(), (now + LIFETIME[purpose]).isoformat()),
        )
    return token


def _within(redis: Redis, key: str, limit: int) -> bool:
    # A counter that starts its hour with the first send.
    redis.set(key, 0, nx=True, ex=3600)
    return int(redis.incr(key)) <= limit


def may_send(redis: Redis, user_id: str, *, forgot: bool) -> bool:
    """Whether another email to this account is allowed now; counts it when it is. Raises RedisError."""
    # Looked at first, so a refusal by the overall cap does not also use up this account's own allowance.
    if forgot and int(redis.get("pwmail:forgot") or 0) >= FORGOT_PER_HOUR:
        return False
    if not redis.set(f"pwmail:cool:{user_id}", 1, nx=True, ex=COOL_DOWN_S):
        return False
    if not _within(redis, f"pwmail:hour:{user_id}", PER_ACCOUNT_PER_HOUR):
        return False
    return not forgot or _within(redis, "pwmail:forgot", FORGOT_PER_HOUR)


def send(deps: AppDeps, user_id: str, purpose: Purpose, *, forgot: bool = False) -> Sent:
    """Email a new link to a user, within the limits. Blocking: callers run it off the event loop."""
    sender = deps.password_mail().sender
    conn = open_connection(path=deps.db_path)
    try:
        user = users.find_by_id(conn, user_id)
        if sender is None or user is None or user.status != "ACTIVE":
            return "FAILED"

        def note(outcome: str) -> None:
            audit.record(conn, actor_user_id=None, actor_email=user.email, action="ACCOUNT_LINK", target_type="user",
                         target_id=user_id, outcome=outcome, ip=None, detail={"purpose": purpose}, clock=deps.clock)
            conn.commit()

        try:
            allowed = may_send(deps.redis, user_id, forgot=forgot)
        except RedisError:
            # Anyone can press "Forgot password?", so uncounted it sends nothing; an administrator's send goes ahead.
            if forgot:
                return "FAILED"
            allowed = True
        if not allowed:
            note("THROTTLED")
            return "THROTTLED"
        # The link the user already has keeps working until this one is on its way.
        token = issue(conn, user_id, purpose, deps.clock, replace=False)
        url = f"{deps.public_url}/set-password#token={token}"
        google = deps.google is not None and len(deps.session_secret) >= MIN_SECRET_BYTES
        try:
            sender.send(account_mail.link_mail(user.email, purpose, url,
                                               f"{deps.public_url}/auth/login" if google else None))
        except Exception as exc:  # noqa: BLE001 - library text can echo the message, so only the type is logged
            _log.warning("password link email not sent: %s", type(exc).__name__)
            # Nobody received this link.
            conn.execute("delete from account_links where token_hash=?", (_hash(token),))
            note("SEND_FAILED")
            try:
                # Nothing arrived, so the next attempt need not wait.
                deps.redis.delete(f"pwmail:cool:{user_id}")
            except RedisError:
                pass
            return "FAILED"
        _cancel_older(conn, user_id, token, deps.clock)
        note("SENT")
        return "SENT"
    finally:
        conn.close()


def _cancel_older(conn: sqlite3.Connection, user_id: str, token: str, clock: Clock) -> None:
    # Only links made before this one, so two sends at once can never cancel each other.
    conn.execute("update account_links set used_at=? where user_id=? and used_at is null "
                 "and rowid < (select rowid from account_links where token_hash=?)",
                 (clock.now().isoformat(), user_id, _hash(token)))


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
