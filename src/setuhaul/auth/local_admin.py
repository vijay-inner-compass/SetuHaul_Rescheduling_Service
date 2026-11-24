"""Optional break-glass admin password: scrypt, per-user salt, lockout, every attempt audited."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

# scrypt for hashing, hmac for the constant-time compare, secrets for the salt.
import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta

from setuhaul.auth import audit, users
from setuhaul.clock import Clock
from setuhaul.db import immediate
from setuhaul.identity import AuthError

MIN_LENGTH = 12
MAX_FAILURES = 5
LOCK_FOR = timedelta(minutes=15)
# scrypt cost: 2**14 is the interactive-login standard (about 16 MiB, tens of milliseconds).
_N, _R, _P = 2**14, 8, 1
# A fixed salt for the dummy work done when the address is unknown, so timing does not reveal it.
_DUMMY_SALT = b"setuhaul-dummy-salt"


def _hash(password: str, salt: bytes) -> str:
    # maxmem is raised above the default 32 MiB so the cost parameters are accepted everywhere.
    return hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=64 * 1024 * 1024).hex()


def set_password(conn: sqlite3.Connection, user_id: str, password: str) -> None:
    # Minimum length is enforced here so no caller can store a weak password.
    if len(password) < MIN_LENGTH:
        raise AuthError("PASSWORD_TOO_SHORT")
    # A fresh random salt per user and per change.
    salt = secrets.token_bytes(16)
    digest = _hash(password, salt)
    # Replacing a password also clears any lockout.
    with immediate(conn):
        conn.execute(
            "insert into admin_credentials(user_id, password_hash, salt, failed_attempts, locked_until) "
            "values (?,?,?,0,NULL) on conflict(user_id) do update set password_hash=excluded.password_hash, "
            "salt=excluded.salt, failed_attempts=0, locked_until=NULL",
            (user_id, digest, salt.hex()),
        )


def _audit(conn: sqlite3.Connection, email: str, user: users.User | None, outcome: str, reason: str | None,
           ip: str | None, clock: Clock) -> None:
    # The detail never carries the password or the hash, only a coarse reason.
    audit.record(conn, actor_user_id=user.user_id if user else None, actor_email=email, action="LOCAL_ADMIN",
                 target_type=None, target_id=None, outcome=outcome, ip=ip,
                 detail={"reason": reason} if reason else None, clock=clock)


def verify(conn: sqlite3.Connection, email: str, password: str, clock: Clock, ip: str | None = None) -> users.User:
    now = clock.now()
    # Look up the account; only an active admin with a stored credential can ever succeed.
    user = users.find_by_email(conn, email)
    cred = None
    if user is not None and user.role == "ADMIN" and user.status == "ACTIVE":
        cred = conn.execute(
            "select password_hash, salt, failed_attempts, locked_until from admin_credentials where user_id=?",
            (user.user_id,),
        ).fetchone()
    # Lockout first: a locked account refuses even the right password, and does no hashing.
    if cred is not None and cred["locked_until"] and datetime.fromisoformat(cred["locked_until"]) > now:
        _audit(conn, email, user, "LOCAL_ADMIN_LOCKED", None, ip, clock)
        conn.commit()
        raise AuthError("LOCKED")
    # Always do one scrypt, against a dummy salt when there is no credential, so work is the same either way.
    salt = bytes.fromhex(cred["salt"]) if cred is not None else _DUMMY_SALT
    candidate = _hash(password, salt)
    stored = cred["password_hash"] if cred is not None else "0" * len(candidate)
    ok = hmac.compare_digest(candidate, stored) and cred is not None
    if ok:
        assert user is not None
        # Success clears the counter and any expired lock.
        with immediate(conn):
            conn.execute("update admin_credentials set failed_attempts=0, locked_until=NULL where user_id=?",
                         (user.user_id,))
            _audit(conn, email, user, "LOCAL_ADMIN_OK", None, ip, clock)
        return user
    # Failure: count it, locking at the limit. An expired lock restarts the count from zero.
    with immediate(conn):
        if cred is not None and user is not None:
            expired = cred["locked_until"] is not None
            failures = (0 if expired else cred["failed_attempts"]) + 1
            locked = (now + LOCK_FOR).isoformat() if failures >= MAX_FAILURES else None
            conn.execute("update admin_credentials set failed_attempts=?, locked_until=? where user_id=?",
                         (failures, locked, user.user_id))
        _audit(conn, email, user, "LOCAL_ADMIN_REFUSED", "BAD_CREDENTIALS", ip, clock)
    raise AuthError("BAD_CREDENTIALS")
