"""Email and password sign-in: one-time links, passwords for every role, and the emails that carry them."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from setuhaul.auth import sessions
from setuhaul.clock import FakeClock
from setuhaul.config import load_settings
from setuhaul.db import MIGRATIONS_DIR, apply_migrations, open_connection
from tests.auth_helpers import World, build_world

MIGRATION = "020_password_sign_in.sql"


@pytest.fixture()
def world(tmp_path: Path, clock: FakeClock) -> World:
    return build_world(tmp_path, clock)


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
