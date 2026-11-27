"""Deleting warehouse supervisors (and other web users): API, UI button, guards and FK preservation."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import fakeredis
from fastapi.testclient import TestClient

from setuhaul.api import AppDeps, create_app
from setuhaul.auth import local_admin, projects, sessions, users
from setuhaul.clock import FakeClock
from setuhaul.db import open_connection


def _setup(db_path: Path, clock: FakeClock) -> tuple[TestClient, users.User, users.User, str, str]:
    conn = open_connection(path=db_path)
    try:
        admin = users.bootstrap_admin(conn, "boss@x.com", clock)
        assert admin is not None
        sup = users.invite(
            conn,
            actor=admin,
            email="sup@x.com",
            role="WAREHOUSE_AUTHORITY",
            display_name="Jaipur Supervisor",
            facilities=("FAC-JAI-01",),
            clock=clock,
        )
        local_admin.set_password(conn, sup.user_id, "supervisor secret pw", clock)
        admin_tok = sessions.create(conn, admin, "GOOGLE", clock)
        sup_tok = sessions.create(conn, sup, "GOOGLE", clock)
        conn.commit()
    finally:
        conn.close()
    deps = AppDeps(
        db_path=db_path,
        redis=fakeredis.FakeRedis(decode_responses=True),
        webhook_secret=b"s",
        coordinator_tokens={},
        fallback_path=db_path.parent / "fb.jsonl",
        clock=clock,
    )
    return TestClient(create_app(deps)), admin, sup, admin_tok, sup_tok


def test_delete_unreferenced_warehouse_supervisor_removes_user_and_revokes_session(
    working_db_path: Path, clock: FakeClock
) -> None:
    client, _admin, sup, admin_tok, sup_tok = _setup(working_db_path, clock)
    # Supervisor session works before deletion.
    client.cookies.set(sessions.COOKIE_NAME, sup_tok)
    assert client.get("/auth/me").status_code == 200

    # Admin deletes supervisor.
    client.cookies.set(sessions.COOKIE_NAME, admin_tok)
    r = client.post(f"/v1/admin/users/{sup.user_id}/delete")
    assert r.status_code == 200
    assert r.json()["user_id"] == sup.user_id

    # Supervisor session is immediately dead.
    client.cookies.set(sessions.COOKIE_NAME, sup_tok)
    assert client.get("/auth/me").status_code == 401

    # Gone from GET /v1/admin/users and can be re-invited with the same email.
    client.cookies.set(sessions.COOKIE_NAME, admin_tok)
    listed = [u["email"] for u in client.get("/v1/admin/users").json()["users"]]
    assert "sup@x.com" not in listed

    reinvite = client.post(
        "/v1/admin/users",
        json={"email": "sup@x.com", "display_name": "New Sup", "role": "WAREHOUSE_AUTHORITY", "facilities": ["FAC-JAI-01"]},
    )
    assert reinvite.status_code == 201

    conn = open_connection(path=working_db_path, readonly=True)
    try:
        assert conn.execute("pragma foreign_key_check").fetchall() == []
        aud = conn.execute("select 1 from audit_log where action='USER_DELETED' and target_id=?", (sup.user_id,)).fetchone()
        assert aud is not None
    finally:
        conn.close()


def test_delete_warehouse_supervisor_with_operational_history_preserves_fks_and_frees_email(
    working_db_path: Path, clock: FakeClock
) -> None:
    client, _admin, sup, admin_tok, sup_tok = _setup(working_db_path, clock)
    conn = open_connection(path=working_db_path)
    try:
        # Supervisor creates a project (projects.created_by is NOT NULL REFERENCES users(user_id)).
        projects.create(
            conn,
            actor=sup,
            customer_name="Acme",
            contract_ref="CNT-DEL-01",
            facility_id="FAC-JAI-01",
            starts_on=date(2026, 8, 4),
            ends_on=None,
            clock=clock,
        )
        conn.commit()
    finally:
        conn.close()

    client.cookies.set(sessions.COOKIE_NAME, admin_tok)
    r = client.post(f"/v1/admin/users/{sup.user_id}/delete")
    assert r.status_code == 200

    # Session dead, hidden from user list, and original email can be re-invited.
    client.cookies.set(sessions.COOKIE_NAME, sup_tok)
    assert client.get("/auth/me").status_code == 401

    client.cookies.set(sessions.COOKIE_NAME, admin_tok)
    listed = [u["email"] for u in client.get("/v1/admin/users").json()["users"]]
    assert "sup@x.com" not in listed

    reinvite = client.post(
        "/v1/admin/users",
        json={"email": "sup@x.com", "display_name": "Rehired Sup", "role": "WAREHOUSE_AUTHORITY", "facilities": ["FAC-JAI-01"]},
    )
    assert reinvite.status_code == 201

    conn = open_connection(path=working_db_path, readonly=True)
    try:
        assert conn.execute("pragma foreign_key_check").fetchall() == []
        assert conn.execute("select count(*) from projects where contract_ref='CNT-DEL-01'").fetchone()[0] == 1
    finally:
        conn.close()


def test_delete_user_guards_and_admin_screen_button(working_db_path: Path, clock: FakeClock) -> None:
    client, admin, sup, admin_tok, sup_tok = _setup(working_db_path, clock)

    # Non-admin cannot delete.
    client.cookies.set(sessions.COOKIE_NAME, sup_tok)
    assert client.post(f"/v1/admin/users/{admin.user_id}/delete").status_code == 403

    # Admin cannot delete self.
    client.cookies.set(sessions.COOKIE_NAME, admin_tok)
    r_self = client.post(f"/v1/admin/users/{admin.user_id}/delete")
    assert r_self.status_code == 409 and r_self.json()["reason_code"] == "CANNOT_DELETE_SELF"

    # Unknown user returns 404.
    r_unk = client.post("/v1/admin/users/USR-NO-SUCH/delete")
    assert r_unk.status_code == 404 and r_unk.json()["reason_code"] == "UNKNOWN_USER"

    # Admin screen shows Delete button for the supervisor, and NOT for the admin themselves.
    html = client.get("/admin").text
    assert f"/v1/admin/users/{sup.user_id}/delete" in html
    assert f"/v1/admin/users/{admin.user_id}/delete" not in html
