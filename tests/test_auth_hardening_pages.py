"""Hardening sweep (11c group E): encoded ids, the same-origin check, demo claim names and the 403 header."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from setuhaul.api import create_app
from setuhaul.auth import admin_api, audit, sessions
from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from setuhaul.demo_ui import build_demo_app
from tests.auth_helpers import JAIPUR, World, build_world, demo_client

ROOT = Path(__file__).resolve().parents[1]
CROSS = {"origin": "https://evil.example"}


@pytest.fixture()
def world(tmp_path: Path, clock: FakeClock) -> World:
    return build_world(tmp_path, clock)


def _audit_rows(world: World, **filters: str) -> list:  # type: ignore[type-arg]
    conn = open_connection(path=world.path, readonly=True)
    try:
        return audit.query(conn, **filters)  # type: ignore[arg-type]
    finally:
        conn.close()


# ---- item 14: ids placed in paths are encoded --------------------------------------------------------------

def test_ids_in_data_api_paths_are_encoded(world: World) -> None:
    nasty, encoded = "../x?y#z", "..%2Fx%3Fy%23z"
    conn = open_connection(path=world.path)
    owner = world.users["wh_a"].user_id
    conn.execute("insert into projects(project_id, customer_name, contract_ref, facility_id, status, created_by)"
                 " values (?,'Acme','NASTY-1',?,'ACTIVE',?)", (nasty, JAIPUR, owner))
    conn.execute("insert into project_drivers(project_id, driver_id, allocated_by, allocated_at) values (?,'DRV001',?,'x')",
                 (nasty, owner))
    # A user whose id needs encoding too.
    conn.execute("insert into users(user_id, email, role, status, display_name, created_at) values (?,?,'ADMIN','ACTIVE','N','x')",
                 ("USR/a?b", "nasty@x.com"))
    conn.commit()
    conn.close()
    page = world.client("admin").get("/warehouse", params={"facility": JAIPUR}).text
    assert f'data-api="/v1/projects/{encoded}/allocate"' in page
    assert f'data-api="/v1/projects/{encoded}/release"' in page
    assert f"/v1/projects/{nasty}" not in page
    admin = world.client("admin").get("/admin").text
    assert 'data-api="/v1/admin/users/USR%2Fa%3Fb/disable"' in admin and "/users/USR/a?b/" not in admin


def test_demo_page_script_encodes_case_ids() -> None:
    page = (ROOT / "src" / "setuhaul" / "demo" / "index.html").read_text(encoding="utf-8")
    assert "/demo/api/cases/${encodeURIComponent(claim.dataset.claim)}/claim" in page
    assert "/demo/api/cases/${encodeURIComponent(id)}/resolve" in page


# ---- item 15: same-origin check for cookie-authenticated unsafe methods ------------------------------------

def _release(client: TestClient, world: World, headers: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    pid = client.get("/v1/projects").json()["projects"][0]["project_id"]
    return client.post(f"/v1/projects/{pid}/release", json={"driver_id": "DRV001"}, headers=headers or {})


@pytest.mark.parametrize("headers", [
    {"origin": "https://evil.example"},
    {"origin": "null"},
    {"origin": "http://localhost:9999"},
    {"sec-fetch-site": "cross-site"},
    {"sec-fetch-site": "same-site"},
    # Sec-Fetch-Site wins even when the Origin looks right.
    {"sec-fetch-site": "cross-site", "origin": "http://localhost"},
])
def test_cross_site_unsafe_request_refused_and_audited(world: World, headers: dict[str, str]) -> None:
    client = world.client("wh_a")
    r = _release(client, world, headers)
    assert r.status_code == 403 and r.json()["reason_code"] == "CROSS_SITE_REQUEST"
    assert r.headers["x-request-id"] == r.json()["request_id"]
    rows = _audit_rows(world, action="CROSS_SITE_REQUEST")
    assert len(rows) == 1 and rows[0]["outcome"] == "FORBIDDEN" and rows[0]["actor_user_id"] == world.users["wh_a"].user_id
    assert rows[0]["target_id"].endswith("/release")
    # Nothing happened: the driver is still allocated.
    assert client.get("/v1/projects").json()["projects"][0]["driver_ids"] == ["DRV001"]


@pytest.mark.parametrize("headers", [
    {},
    {"origin": "http://localhost"},
    {"origin": "http://localhost:80"},
    {"sec-fetch-site": "same-origin"},
    {"sec-fetch-site": "none"},
    {"sec-fetch-site": "same-origin", "origin": "https://evil.example"},
])
def test_same_origin_or_headerless_request_allowed(world: World, headers: dict[str, str]) -> None:
    assert _release(world.client("wh_a"), world, headers).status_code == 200


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_other_unsafe_methods_are_checked_and_safe_methods_are_not(world: World, method: str) -> None:
    client = world.client("wh_a")
    assert client.request(method, "/v1/projects", headers=CROSS).json()["reason_code"] == "CROSS_SITE_REQUEST"
    assert client.get("/v1/projects", headers={**CROSS, "sec-fetch-site": "cross-site"}).status_code == 200


def test_public_url_host_is_the_expected_origin(world: World) -> None:
    world.deps.public_url = "https://haul.example.com"
    app = create_app(world.deps)
    client = TestClient(app, base_url="http://internal:8000", follow_redirects=False)
    client.cookies.set(sessions.COOKIE_NAME, world.client("wh_a").cookies[sessions.COOKIE_NAME])
    assert client.post("/auth/logout", headers={"origin": "http://internal:8000"}).status_code == 403
    assert client.post("/auth/logout", headers={"origin": "https://haul.example.com"}).status_code == 200


def test_channel_routes_without_a_session_cookie_are_unaffected(world: World) -> None:
    world.deps.telegram_secret = b"t" * 20
    app = create_app(world.deps)
    client = TestClient(app, base_url="http://localhost", follow_redirects=False)
    headers = {**CROSS, "sec-fetch-site": "cross-site", "x-telegram-bot-api-secret-token": "t" * 20}
    hook = client.post("/v1/telegram/webhook", json={}, headers=headers)
    assert hook.status_code != 403 and hook.json().get("reason_code") != "CROSS_SITE_REQUEST"
    body = b"{}"
    signed = client.post("/v1/driver/messages", content=body, headers={
        **CROSS, "x-signature": hmac.new(world.deps.webhook_secret, body, hashlib.sha256).hexdigest()})
    assert signed.json().get("reason_code") != "CROSS_SITE_REQUEST"
    # A legacy bearer coordinator call carries no cookie either.
    bearer = client.post("/v1/coordinator/cases/NOPE/claim", headers={**CROSS, "authorization": "Bearer tok-asha"})
    assert bearer.json().get("reason_code") != "CROSS_SITE_REQUEST"
    assert _audit_rows(world, action="CROSS_SITE_REQUEST") == []


# ---- item 16: the demo console cannot claim as a web identity -----------------------------------------------

def test_demo_claim_refuses_a_web_identity(tmp_path: Path) -> None:
    app, _ = build_demo_app(tmp_path / "demo")
    client = demo_client(app, base_url="http://localhost", client=("127.0.0.1", 5000))
    r = client.post("/demo/api/cases/ESC-NOPE/claim", json={"coordinator": "web:boss@x.com"})
    assert r.status_code == 422
    assert client.post("/demo/api/cases/ESC-NOPE/claim", json={"coordinator": "Asha Verma"}).status_code != 422


# ---- item 17: public parse_since and the 403 header --------------------------------------------------------

def test_parse_since_is_public() -> None:
    assert admin_api.parse_since("2026-08-04T09:00:00+05:30") == "2026-08-04T09:00:00+05:30"
    assert not hasattr(admin_api, "_parse_since")


@pytest.mark.parametrize(("who", "path"), [("wh_a", "/admin"), ("wh_none", "/warehouse")])
def test_forbidden_page_shows_who_is_signed_in_and_sign_out(world: World, who: str, path: str) -> None:
    r = world.client(who).get(path)
    assert r.status_code == 403
    assert world.users[who].email in r.text and 'data-api="/auth/logout"' in r.text
