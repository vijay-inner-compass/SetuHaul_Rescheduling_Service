"""Email and password sign-in: one-time links, passwords for every role, and the emails that carry them."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from setuhaul.auth import audit, local_admin, sessions, users
from setuhaul.clock import FakeClock
from setuhaul.config import load_settings
from setuhaul.db import MIGRATIONS_DIR, apply_migrations, open_connection
from setuhaul.mail_transport import InMemoryMailbox
from tests.auth_helpers import World, build_world

MIGRATION = "020_password_sign_in.sql"
PASSWORD = "correct horse battery"
BASE = "http://localhost"


@pytest.fixture()
def world(tmp_path: Path, clock: FakeClock) -> World:
    # The world with password sign-in switched on and mail kept in memory.
    built = build_world(tmp_path, clock)
    built.deps.password_sender = InMemoryMailbox()
    built.deps.public_url = BASE
    return built


def _set_password(world: World, who: str, password: str = PASSWORD) -> None:
    conn = open_connection(path=world.path)
    try:
        local_admin.set_password(conn, world.users[who].user_id, password, world.clock)
    finally:
        conn.close()


def _login(world: World, email: str = "wha@x.com", password: str = PASSWORD):  # type: ignore[no-untyped-def]
    return world.client().post("/auth/password", json={"email": email, "password": password})


def _query(world: World, sql: str, *args: object) -> list[tuple[object, ...]]:
    conn = open_connection(path=world.path)
    try:
        rows = [tuple(r) for r in conn.execute(sql, args)]
        conn.commit()
        return rows
    finally:
        conn.close()


# ---- migration and session ---------------------------------------------------------------------------------
def test_migration_keeps_existing_sessions(tmp_path: Path) -> None:
    older = tmp_path / "older"
    older.mkdir()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        if path.name < MIGRATION:
            shutil.copyfile(path, older / path.name)
    db = tmp_path / "old.sqlite3"
    shutil.copyfile(load_settings().db_path, db)
    conn = open_connection(path=db)
    try:
        apply_migrations(conn, older)
        conn.execute("insert into users(user_id, email, role, created_at) values ('U1','a@x.com','ADMIN','t')")
        conn.execute("insert into auth_sessions(session_id, user_id, created_at, expires_at, auth_method) "
                     "values ('s1','U1','c','e','GOOGLE')")
        conn.commit()
        assert apply_migrations(conn) == [MIGRATION]
        assert [tuple(r) for r in conn.execute("select * from auth_sessions")] == [("s1", "U1", "c", "e", None, "GOOGLE")]
        indexes = {r[0] for r in conn.execute("select name from sqlite_master where type='index'")}
        assert "ix_auth_sessions_user" in indexes
    finally:
        conn.close()


def test_password_session_lasts_8_hours(world: World) -> None:
    conn = open_connection(path=world.path)
    try:
        token = sessions.create(conn, world.users["wh_a"], "PASSWORD", world.clock)
        world.clock.advance(timedelta(hours=7, minutes=59))
        assert sessions.resolve(conn, token, world.clock) is not None
        world.clock.advance(timedelta(minutes=2))
        assert sessions.resolve(conn, token, world.clock) is None
    finally:
        conn.close()


# ---- password sign-in --------------------------------------------------------------------------------------
def test_warehouse_user_signs_in(world: World) -> None:
    _set_password(world, "wh_a")
    client = world.client()
    r = client.post("/auth/password", json={"email": "wha@x.com", "password": PASSWORD})
    assert r.status_code == 200
    assert "Max-Age=28800" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
    assert client.get("/auth/me").json()["role"] == "WAREHOUSE_AUTHORITY"
    assert _query(world, "select auth_method from auth_sessions where auth_method='PASSWORD'") == [("PASSWORD",)]
    conn = open_connection(path=world.path)
    try:
        assert [r["outcome"] for r in audit.query(conn, action="PASSWORD_LOGIN")] == ["PASSWORD_OK"]
    finally:
        conn.close()


def test_same_refusal_for_unknown_disabled_passwordless_and_locked(world: World) -> None:
    _set_password(world, "wh_a")
    _set_password(world, "wh_b")
    conn = open_connection(path=world.path)
    try:
        users.disable(conn, actor=world.users["admin"], user_id=world.users["wh_b"].user_id, clock=world.clock)
    finally:
        conn.close()
    for _ in range(5):
        _login(world, password="wrong password!!")
    replies = [_login(world, "nobody@x.com"), _login(world, "whb@x.com"), _login(world, "boss@x.com"), _login(world)]
    assert [(r.status_code, r.json()["reason_code"]) for r in replies] == [(401, "BAD_CREDENTIALS")] * 4


def test_email_is_case_and_space_insensitive(world: World) -> None:
    _set_password(world, "wh_a")
    assert _login(world, "  WHA@X.com ").status_code == 200


def test_five_failures_lock_15_minutes(world: World) -> None:
    _set_password(world, "wh_a")
    for _ in range(5):
        assert _login(world, password="wrong password!!").status_code == 401
    world.clock.advance(timedelta(minutes=14))
    assert _login(world).status_code == 401
    world.clock.advance(timedelta(minutes=2))
    assert _login(world).status_code == 200


def test_off_is_404(world: World) -> None:
    _set_password(world, "wh_a")
    world.deps.password_sender = None
    assert _login(world).status_code == 404


def test_bad_bodies_are_refused(world: World) -> None:
    client = world.client()
    bodies = [b"not json", b"[]", b"{}", b'{"email": 1, "password": 2}',
              b'{"email": "wha@x.com", "password": "' + b"x" * 1025 + b'"}',
              b'{"email": "wha@x.com", "password": "\\ud800aaaaaaaaaaaa"}',
              b'{"email": "\\ud800@x.com", "password": "aaaaaaaaaaaaaaa"}']
    for body in bodies:
        r = client.post("/auth/password", content=body, headers={"content-type": "application/json"})
        assert (r.status_code, r.json()["reason_code"]) == (401, "BAD_CREDENTIALS"), body
