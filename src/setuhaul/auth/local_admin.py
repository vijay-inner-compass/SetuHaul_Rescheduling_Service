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
from setuhaul.clock import Clock, SystemClock
from setuhaul.db import immediate
from setuhaul.identity import AuthError

MIN_LENGTH = 12
MAX_LENGTH = 1024
MAX_FAILURES = 5
# Distributed-attack safety net when per-IP lockouts are active: 4+ distinct IPs cannot guess indefinitely,
# while a single stranger IP (locked at 5 tries on their own IP) cannot lock an admin out from another IP.
GLOBAL_MAX_FAILURES = 20
LOCK_FOR = timedelta(minutes=15)
# scrypt cost: 2**14 is the interactive-login standard (about 16 MiB, tens of milliseconds).
_N, _R, _P = 2**14, 8, 1
# A fixed salt for the dummy work done when the address is unknown, so timing does not reveal it.
_DUMMY_SALT = b"setuhaul-dummy-salt"


def _hash(password: str, salt: bytes, n: int = _N, r: int = _R, p: int = _P) -> str:
    # maxmem is raised above the default 32 MiB so the cost parameters are accepted everywhere.
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024).hex()


def _encode(password: str, salt: bytes) -> str:
    # Self-describing, so the cost can be raised later without breaking stored hashes.
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${_hash(password, salt)}"


def _check(password: str, stored: str) -> bool:
    # Parse the parameters out of the stored value; anything malformed simply fails.
    try:
        scheme, n, r, p, salt_hex, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = _hash(password, bytes.fromhex(salt_hex), int(n), int(r), int(p))
        # A tampered digest (absurd cost, non-ASCII text) fails closed instead of raising.
        return hmac.compare_digest(candidate, digest)
    except (ValueError, TypeError, OverflowError):
        return False


def hash_new_password(password: str) -> tuple[str, str]:
    """(stored hash, salt hex) for a new password; the slow part, so callers can run it outside the write lock."""
    # Minimum length is enforced here so no caller can store a weak password.
    if len(password) < MIN_LENGTH:
        raise AuthError("PASSWORD_TOO_SHORT")
    # The upper bound keeps hashing cost bounded.
    if len(password) > MAX_LENGTH:
        raise AuthError("PASSWORD_TOO_LONG")
    # A fresh random salt per user and per change.
    salt = secrets.token_bytes(16)
    return _encode(password, salt), salt.hex()


def store_password(conn: sqlite3.Connection, user_id: str, stored: str, salt_hex: str) -> None:
    """Write a hashed password inside the caller's transaction. Replacing a password also clears any lockout."""
    conn.execute(
        "insert into admin_credentials(user_id, password_hash, salt, failed_attempts, locked_until) "
        "values (?,?,?,0,NULL) on conflict(user_id) do update set password_hash=excluded.password_hash, "
        "salt=excluded.salt, failed_attempts=0, locked_until=NULL",
        (user_id, stored, salt_hex),
    )
    conn.execute("delete from auth_ip_lockouts where user_id=?", (user_id,))


def set_password(conn: sqlite3.Connection, user_id: str, password: str, clock: Clock | None = None) -> None:
    stored, salt_hex = hash_new_password(password)
    # The change is audited without any secret.
    with immediate(conn):
        store_password(conn, user_id, stored, salt_hex)
        audit.record(conn, actor_user_id=None, actor_email=None, action="LOCAL_ADMIN", target_type="user",
                     target_id=user_id, outcome="LOCAL_ADMIN_PASSWORD_SET", ip=None, detail=None, clock=clock or SystemClock())


def _audit(conn: sqlite3.Connection, email: str, user: users.User | None, outcome: str, reason: str | None,
           ip: str | None, clock: Clock, admin_only: bool) -> None:
    # The break-glass route and the everyday password sign-in are told apart in the log.
    action, prefix = ("LOCAL_ADMIN", "LOCAL_ADMIN") if admin_only else ("PASSWORD_LOGIN", "PASSWORD")
    # The detail never carries the password or the hash, only a coarse reason.
    audit.record(conn, actor_user_id=user.user_id if user else None, actor_email=email, action=action,
                 target_type=None, target_id=None, outcome=f"{prefix}_{outcome}", ip=ip,
                 detail={"reason": reason} if reason else None, clock=clock)


def _ip_locked(conn: sqlite3.Connection, user_id: str, ip: str | None, now: datetime) -> bool:
    if not ip:
        return False
    row = conn.execute(
        "select locked_until from auth_ip_lockouts where user_id=? and ip=?", (user_id, ip)
    ).fetchone()
    return row is not None and _is_locked(row["locked_until"], now)


def _is_locked(locked_until: str | None, now: datetime) -> bool:
    return bool(locked_until) and datetime.fromisoformat(str(locked_until)) > now


def verify(conn: sqlite3.Connection, email: str, password: str, clock: Clock, ip: str | None = None, *,
           admin_only: bool = True) -> users.User:
    # Look up the account; only an active user (an admin on the break-glass route) with a stored credential can succeed.
    user = users.find_by_email(conn, email)
    cred = None
    if user is not None and user.status == "ACTIVE" and (user.role == "ADMIN" or not admin_only):
        cred = conn.execute(
            "select password_hash, locked_until from admin_credentials where user_id=?", (user.user_id,)
        ).fetchone()
    # Early exit: a locked account refuses even the right password.
    now_check = clock.now()
    if cred is not None and user is not None and (_is_locked(cred["locked_until"], now_check) or _ip_locked(conn, user.user_id, ip, now_check)):
        # The same dummy scrypt as the unknown-address path, so locked and unknown take comparable time.
        _hash("x" * 12, _DUMMY_SALT)
        _audit(conn, email, user, "LOCKED", None, ip, clock, admin_only)
        conn.commit()
        raise AuthError("LOCKED")
    # The slow hash runs outside the write lock; an over-long password is refused without hashing it.
    too_long = len(password) > MAX_LENGTH
    if cred is not None and not too_long:
        ok = _check(password, cred["password_hash"])
    else:
        # Same amount of work for unknown, ineligible and over-long attempts.
        _hash("x" * 12, _DUMMY_SALT)
        ok = False
    # The decision and the counter update are one atomic step, so parallel attempts serialise here.
    with immediate(conn):
        fresh = None
        if cred is not None and user is not None:
            fresh = conn.execute(
                "select password_hash, locked_until from admin_credentials where user_id=?", (user.user_id,)
            ).fetchone()
        now = clock.now()
        if fresh is not None and user is not None and (_is_locked(fresh["locked_until"], now) or _ip_locked(conn, user.user_id, ip, now)):
            # A lock landed while this attempt was hashing: refuse, even if the password was right.
            outcome, reason = "LOCKED", None
        elif ok and user is not None and fresh is not None and cred is not None and fresh["password_hash"] == cred["password_hash"]:
            conn.execute("update admin_credentials set failed_attempts=0, locked_until=NULL where user_id=?",
                         (user.user_id,))
            if ip:
                conn.execute("delete from auth_ip_lockouts where user_id=? and ip=?", (user.user_id, ip))
            outcome, reason = "OK", None
        else:
            if fresh is not None and user is not None:
                # Atomic increment in SQL; an expired lock restarts the count at one.
                conn.execute(
                    "update admin_credentials set "
                    "failed_attempts = case when locked_until is not null then 1 else failed_attempts + 1 end, "
                    "locked_until = case when (case when locked_until is not null then 1 else failed_attempts + 1 end) >= ? "
                    "then ? else NULL end where user_id=?",
                    (GLOBAL_MAX_FAILURES if ip else MAX_FAILURES, (now + LOCK_FOR).isoformat(), user.user_id),
                )
                if ip:
                    conn.execute(
                        "insert into auth_ip_lockouts(user_id, ip, failed_attempts, locked_until) values (?,?,1,NULL) "
                        "on conflict(user_id, ip) do update set "
                        "failed_attempts = case when locked_until is not null then 1 else failed_attempts + 1 end, "
                        "locked_until = case when (case when locked_until is not null then 1 else failed_attempts + 1 end) >= ? "
                        "then ? else NULL end",
                        (user.user_id, ip, MAX_FAILURES, (now + LOCK_FOR).isoformat()),
                    )
            outcome, reason = "REFUSED", "BAD_CREDENTIALS"
        _audit(conn, email, user, outcome, reason, ip, clock, admin_only)
    if outcome == "LOCKED":
        raise AuthError("LOCKED")
    if outcome == "REFUSED":
        raise AuthError("BAD_CREDENTIALS")
    assert user is not None
    return user
