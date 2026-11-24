"""Invite-only users, bootstrap admin and disable."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import sqlite3

import pytest

from setuhaul.auth import audit, users
from setuhaul.clock import FakeClock
from setuhaul.identity import AuthError


def _admin(db: sqlite3.Connection, clock: FakeClock) -> users.User:
    # The first admin comes from bootstrap, as in production.
    return users.bootstrap_admin(db, "Boss@X.com", clock)


def test_invite_requires_admin(db: sqlite3.Connection, clock: FakeClock) -> None:
    admin = _admin(db, clock)
    wa = users.invite(db, actor=admin, email="wa@x.com", role="ADMIN", display_name="W", clock=clock)
    # A non-admin actor (here a warehouse-authority-shaped user) is refused.
    fac = db.execute("select facility_id from facilities limit 1").fetchone()[0]
    wa_user = users.invite(
        db, actor=admin, email="auth@x.com", role="WAREHOUSE_AUTHORITY", display_name="A", facilities=(fac,), clock=clock
    )
    with pytest.raises(AuthError) as exc:
        users.invite(db, actor=wa_user, email="z@x.com", role="ADMIN", display_name="Z", clock=clock)
    assert exc.value.code == "FORBIDDEN"
    assert wa.role == "ADMIN"


def test_warehouse_authority_needs_existing_facility(db: sqlite3.Connection, clock: FakeClock) -> None:
    admin = _admin(db, clock)
    for facs in ((), ("NOPE",)):
        with pytest.raises(AuthError):
            users.invite(
                db, actor=admin, email="w@x.com", role="WAREHOUSE_AUTHORITY", display_name="W", facilities=facs, clock=clock
            )


def test_email_match_is_case_insensitive(db: sqlite3.Connection, clock: FakeClock) -> None:
    admin = _admin(db, clock)
    users.invite(db, actor=admin, email=" Vijay@X.com ", role="ADMIN", display_name="V", clock=clock)
    found = users.find_by_email(db, "vijay@x.com")
    assert found is not None and found.email == "vijay@x.com"
    assert users.find_by_email(db, "VIJAY@X.COM") == found


def test_bootstrap_admin_is_idempotent(db: sqlite3.Connection, clock: FakeClock) -> None:
    first = users.bootstrap_admin(db, "a@x.com", clock)
    second = users.bootstrap_admin(db, "A@x.com", clock)
    assert first == second and first.role == "ADMIN"
    assert db.execute("select count(*) from users").fetchone()[0] == 1


def test_disable_revokes_sessions(db: sqlite3.Connection, clock: FakeClock) -> None:
    admin = _admin(db, clock)
    victim = users.invite(db, actor=admin, email="v@x.com", role="ADMIN", display_name="V", clock=clock)
    db.execute(
        "insert into auth_sessions(session_id, user_id, created_at, expires_at, auth_method) values ('s1',?,?,?,'GOOGLE')",
        (victim.user_id, "2026-08-04T09:00:00+05:30", "2026-08-04T17:00:00+05:30"),
    )
    users.disable(db, actor=admin, user_id=victim.user_id, clock=clock)
    assert db.execute("select revoked_at from auth_sessions where session_id='s1'").fetchone()[0] is not None
    found = users.find_by_email(db, "v@x.com")
    assert found is not None and found.status == "DISABLED"


def test_invite_writes_audit_row(db: sqlite3.Connection, clock: FakeClock) -> None:
    admin = _admin(db, clock)
    users.invite(db, actor=admin, email="n@x.com", role="ADMIN", display_name="N", clock=clock)
    rows = audit.query(db, action="INVITE")
    assert len(rows) == 1 and rows[0]["target_id"] is not None
