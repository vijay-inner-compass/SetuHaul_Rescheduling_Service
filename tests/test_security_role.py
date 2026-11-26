"""The security role: invited with facilities, sees the gate and nothing else."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from setuhaul.auth import users
from setuhaul.clock import FakeClock
from setuhaul.db import MIGRATIONS_DIR, apply_migrations, open_connection
from setuhaul.identity import AuthError
from tests.auth_helpers import JAIPUR, World, add_user, build_world
from tests.test_auth_role_matrix import HTML_GUARDED, ROLE_GUARDED


@pytest.fixture()
def world(tmp_path: Path, clock: FakeClock) -> World:
    w = build_world(tmp_path, clock)
    conn = open_connection(path=w.path)
    w.users["security"] = add_user(conn, clock, "guard@x.com", "SECURITY", (JAIPUR,))
    w.users["security_none"] = add_user(conn, clock, "guard2@x.com", "SECURITY", ())
    conn.close()
    return w


def test_migration_keeps_existing_users(tmp_path: Path) -> None:
    before = tmp_path / "before"
    before.mkdir()
    for script in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if script.name < "017":
            shutil.copy(script, before / script.name)
    db = tmp_path / "m.sqlite3"
    shutil.copyfile(Path("data/setuhaul.sqlite3"), db)
    conn = open_connection(path=db)
    apply_migrations(conn, before)
    conn.execute("insert into users(user_id, email, role, status, created_at) values ('U1','a@x.com','ADMIN','ACTIVE','t')")
    conn.execute("insert into auth_sessions(session_id, user_id, created_at, expires_at, auth_method) values ('S1','U1','t','t','GOOGLE')")
    conn.commit()
    rows = [tuple(r) for r in conn.execute("select * from users")]
    apply_migrations(conn)
    assert [tuple(r) for r in conn.execute("select * from users")] == rows
    assert conn.execute("pragma foreign_key_check").fetchall() == []
    conn.execute("insert into users(user_id, email, role, status, created_at) values ('U2','b@x.com','SECURITY','ACTIVE','t')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("insert into users(user_id, email, role, status, created_at) values ('U3','c@x.com','GUARD','ACTIVE','t')")
    # The address stays unique whatever its case, and sessions still point at their user.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("insert into users(user_id, email, role, status, created_at) values ('U4','A@X.com','ADMIN','ACTIVE','t')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("insert into auth_sessions(session_id, user_id, created_at, expires_at, auth_method) values ('S2','NOPE','t','t','GOOGLE')")
    conn.close()


def test_admin_invites_security_with_facilities(world: World) -> None:
    conn = open_connection(path=world.path)
    try:
        made = users.invite(conn, actor=world.users["admin"], email="g@x.com", role="SECURITY", display_name="G",
                            facilities=(JAIPUR,), clock=world.clock)
        assert made.role == "SECURITY" and made.facilities == (JAIPUR,)
        with pytest.raises(AuthError, match="FACILITY_REQUIRED"):
            users.invite(conn, actor=world.users["admin"], email="h@x.com", role="SECURITY", display_name="H", clock=world.clock)
    finally:
        conn.close()
    assert 'value="SECURITY"' in world.client("admin").get("/admin").text


def test_security_is_refused_everywhere_else(world: World) -> None:
    client = world.client("security")
    checked = 0
    for (method, path), roles in {**ROLE_GUARDED, **HTML_GUARDED}.items():
        if "SECURITY" in roles or path.startswith("/demo"):  # the demo console is not part of this app
            continue
        url = path.replace("{", "").replace("}", "")
        response = client.request(method, url, json={} if method == "POST" else None)
        # A body that fails validation is 422 only after the role check has passed, so 403 is the proof.
        assert response.status_code in (403, 422), (method, path, response.status_code)
        if response.status_code == 422:
            assert world.client("security_none").request(method, url, json={}).status_code in (403, 422)
        checked += 1
    assert checked > 40
    assert client.get("/warehouse").status_code == 403 and client.get("/v1/warehouse/drivers").status_code == 403


def test_security_lands_on_the_gate(world: World) -> None:
    response = world.client("security").get("/")
    assert response.status_code == 303 and response.headers["location"] == "/gate"
