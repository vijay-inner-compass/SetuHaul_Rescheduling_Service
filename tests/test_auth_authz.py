"""Role guard and facility scope: server-side, cookie-only, re-read on every request."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from pathlib import Path
from typing import Annotated

import fakeredis
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from setuhaul.api import AppDeps, create_app
from setuhaul.auth import audit, authz, sessions, users
from setuhaul.auth.users import User
from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection


def _make(tmp_path: Path, clock: FakeClock):  # type: ignore[no-untyped-def]
    # A database with an admin and a warehouse authority bound to the first facility only.
    path = make_working_db(tmp_path / "z.sqlite3")
    conn = open_connection(path=path)
    facs = [r[0] for r in conn.execute("select facility_id from facilities order by facility_id limit 2")]
    admin = users.bootstrap_admin(conn, "boss@x.com", clock)
    wh = users.invite(conn, actor=admin, email="wh@x.com", role="WAREHOUSE_AUTHORITY", display_name="W",
                      facilities=tuple(facs[:1]), clock=clock)
    tokens = {"admin": sessions.create(conn, admin, "GOOGLE", clock), "wh": sessions.create(conn, wh, "GOOGLE", clock)}
    conn.close()
    deps = AppDeps(db_path=path, redis=fakeredis.FakeRedis(decode_responses=True), webhook_secret=b"w",
                   coordinator_tokens={}, fallback_path=tmp_path / "f.jsonl", clock=clock, session_secret=b"s" * 32)
    app = create_app(deps)

    # One route per role, plus one that exercises the facility helpers.
    @app.get("/t/admin")
    def admin_only(user: Annotated[User, Depends(authz.require_role("ADMIN"))]) -> dict[str, object]:
        return {"email": user.email}

    @app.get("/t/wh")
    def wh_only(user: Annotated[User, Depends(authz.require_role("WAREHOUSE_AUTHORITY"))]) -> dict[str, object]:
        return {"email": user.email}

    @app.get("/t/either")
    def either(user: Annotated[User, Depends(authz.require_role("ADMIN", "WAREHOUSE_AUTHORITY"))]) -> dict[str, object]:
        return {"scope": authz.facility_ids(user)}

    @app.get("/t/fac/{facility_id}")
    def fac(facility_id: str, user: Annotated[User, Depends(authz.require_role("ADMIN", "WAREHOUSE_AUTHORITY"))]) -> dict[str, str]:
        authz.assert_facility(user, facility_id)
        return {"ok": facility_id}

    client = TestClient(app, base_url="http://localhost", follow_redirects=False)
    return path, client, tokens, facs


def _as(client: TestClient, token: str) -> None:
    client.cookies.set(sessions.COOKIE_NAME, token)


def test_no_cookie_is_401(tmp_path: Path, clock: FakeClock) -> None:
    _, client, _, _ = _make(tmp_path, clock)
    r = client.get("/t/admin")
    assert r.status_code == 401
    assert r.json()["reason_code"] == "UNAUTHENTICATED" and r.json()["request_id"]


def test_wrong_role_is_403_and_audited(tmp_path: Path, clock: FakeClock) -> None:
    path, client, tokens, _ = _make(tmp_path, clock)
    _as(client, tokens["wh"])
    r = client.get("/t/admin")
    assert r.status_code == 403 and r.json()["reason_code"] == "FORBIDDEN"
    conn = open_connection(path=path, readonly=True)
    rows = audit.query(conn, action="ACCESS_DENIED")
    conn.close()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "FORBIDDEN" and rows[0]["target_id"] == "/t/admin"
    assert rows[0]["actor_email"] == "wh@x.com"
    # No secrets in the stored row.
    assert tokens["wh"] not in " ".join(str(dict(rows[0])).split())


def test_unauthenticated_is_not_audited(tmp_path: Path, clock: FakeClock) -> None:
    path, client, _, _ = _make(tmp_path, clock)
    client.get("/t/admin")
    conn = open_connection(path=path, readonly=True)
    assert audit.query(conn, action="ACCESS_DENIED") == []
    conn.close()


def test_right_role_is_200(tmp_path: Path, clock: FakeClock) -> None:
    _, client, tokens, _ = _make(tmp_path, clock)
    _as(client, tokens["admin"])
    assert client.get("/t/admin").json() == {"email": "boss@x.com"}
    _as(client, tokens["wh"])
    assert client.get("/t/wh").json() == {"email": "wh@x.com"}


def test_warehouse_scoped_to_own_facilities(tmp_path: Path, clock: FakeClock) -> None:
    _, client, tokens, facs = _make(tmp_path, clock)
    _as(client, tokens["wh"])
    assert client.get("/t/either").json()["scope"] == facs[:1]
    assert client.get(f"/t/fac/{facs[0]}").status_code == 200
    r = client.get(f"/t/fac/{facs[1]}")
    assert r.status_code == 403 and r.json()["reason_code"] == "FORBIDDEN"


def test_admin_sees_all_facilities(tmp_path: Path, clock: FakeClock) -> None:
    _, client, tokens, facs = _make(tmp_path, clock)
    _as(client, tokens["admin"])
    assert client.get("/t/either").json()["scope"] is None
    assert client.get(f"/t/fac/{facs[1]}").status_code == 200


def test_disabled_user_cookie_is_401(tmp_path: Path, clock: FakeClock) -> None:
    path, client, tokens, _ = _make(tmp_path, clock)
    _as(client, tokens["admin"])
    assert client.get("/t/admin").status_code == 200
    conn = open_connection(path=path)
    conn.execute("update users set status='DISABLED' where email='boss@x.com'")
    conn.commit()
    conn.close()
    assert client.get("/t/admin").status_code == 401


def test_role_change_applies_on_next_request(tmp_path: Path, clock: FakeClock) -> None:
    path, client, tokens, _ = _make(tmp_path, clock)
    _as(client, tokens["admin"])
    assert client.get("/t/admin").status_code == 200
    conn = open_connection(path=path)
    conn.execute("update users set role='WAREHOUSE_AUTHORITY' where email='boss@x.com'")
    conn.commit()
    conn.close()
    assert client.get("/t/admin").status_code == 403


def test_tampered_cookie_is_401(tmp_path: Path, clock: FakeClock) -> None:
    _, client, tokens, _ = _make(tmp_path, clock)
    _as(client, tokens["admin"][:-1] + ("A" if tokens["admin"][-1] != "A" else "B"))
    assert client.get("/t/admin").status_code == 401


def test_headers_and_query_cannot_grant_a_role(tmp_path: Path, clock: FakeClock) -> None:
    _, client, tokens, _ = _make(tmp_path, clock)
    _as(client, tokens["wh"])
    r = client.get("/t/admin?role=ADMIN", headers={"X-Role": "ADMIN", "X-User-Email": "boss@x.com"})
    assert r.status_code == 403


def test_assert_facility_unit() -> None:
    wh = User(user_id="u", email="e", role="WAREHOUSE_AUTHORITY", status="ACTIVE", display_name=None, facilities=("A",))
    authz.assert_facility(wh, "A")
    with pytest.raises(authz.AuthzError) as exc:
        authz.assert_facility(wh, "B")
    assert exc.value.status == 403 and exc.value.code == "FORBIDDEN"
