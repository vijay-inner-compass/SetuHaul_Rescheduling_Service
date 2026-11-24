"""Hashed cookie sessions: expiry, revocation, and live user re-reads."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import sqlite3
from datetime import timedelta

from setuhaul.auth import sessions, users
from setuhaul.clock import FakeClock


def _admin(db: sqlite3.Connection, clock: FakeClock) -> users.User:
    # The first admin comes from bootstrap, as in production.
    return users.bootstrap_admin(db, "boss@x.com", clock)


def test_resolve_valid_token(db: sqlite3.Connection, clock: FakeClock) -> None:
    admin = _admin(db, clock)
    token = sessions.create(db, admin, "GOOGLE", clock)
    # A fresh token resolves to the same user.
    assert sessions.resolve(db, token, clock) == admin


def test_expired_session_is_none(db: sqlite3.Connection, clock: FakeClock) -> None:
    token = sessions.create(db, _admin(db, clock), "GOOGLE", clock)
    # Still valid just before 8 hours, gone just after.
    clock.advance(timedelta(hours=7, minutes=59))
    assert sessions.resolve(db, token, clock) is not None
    clock.advance(timedelta(minutes=2))
    assert sessions.resolve(db, token, clock) is None


def test_break_glass_session_expires_after_one_hour(db: sqlite3.Connection, clock: FakeClock) -> None:
    token = sessions.create(db, _admin(db, clock), "LOCAL_ADMIN", clock)
    clock.advance(timedelta(minutes=59))
    assert sessions.resolve(db, token, clock) is not None
    clock.advance(timedelta(minutes=2))
    assert sessions.resolve(db, token, clock) is None


def test_revoked_is_none(db: sqlite3.Connection, clock: FakeClock) -> None:
    token = sessions.create(db, _admin(db, clock), "GOOGLE", clock)
    sessions.revoke(db, token, clock)
    assert sessions.resolve(db, token, clock) is None


def test_unknown_token_is_none(db: sqlite3.Connection, clock: FakeClock) -> None:
    assert sessions.resolve(db, "nope", clock) is None


def test_disabled_user_session_is_none(db: sqlite3.Connection, clock: FakeClock) -> None:
    admin = _admin(db, clock)
    other = users.invite(db, actor=admin, email="o@x.com", role="ADMIN", display_name="O", clock=clock)
    token = sessions.create(db, other, "GOOGLE", clock)
    # Flip the status directly (without revoking) to prove resolve re-reads the user.
    db.execute("update users set status='DISABLED' where user_id=?", (other.user_id,))
    assert sessions.resolve(db, token, clock) is None


def test_role_change_takes_effect_on_next_resolve(db: sqlite3.Connection, clock: FakeClock) -> None:
    admin = _admin(db, clock)
    token = sessions.create(db, admin, "GOOGLE", clock)
    fac = db.execute("select facility_id from facilities limit 1").fetchone()[0]
    db.execute("update users set role='WAREHOUSE_AUTHORITY' where user_id=?", (admin.user_id,))
    db.execute("insert into user_facilities(user_id, facility_id) values (?,?)", (admin.user_id, fac))
    resolved = sessions.resolve(db, token, clock)
    assert resolved is not None and resolved.role == "WAREHOUSE_AUTHORITY" and resolved.facilities == (fac,)


def test_raw_token_is_not_stored(db: sqlite3.Connection, clock: FakeClock) -> None:
    token = sessions.create(db, _admin(db, clock), "GOOGLE", clock)
    rows = db.execute("select * from auth_sessions").fetchall()
    assert len(rows) == 1
    # The token appears in no column; the id is its sha256 hex.
    assert all(token not in str(v) for v in tuple(rows[0]))
    assert len(rows[0]["session_id"]) == 64


def test_cookie_kwargs() -> None:
    assert sessions.COOKIE_NAME == "setuhaul_session"
    assert sessions.cookie_kwargs(True) == {"httponly": True, "samesite": "lax", "secure": True, "path": "/"}
    assert sessions.cookie_kwargs(False)["secure"] is False
