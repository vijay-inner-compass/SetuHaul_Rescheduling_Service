"""Role matrix: every route the app serves is classified once, and each class is enforced."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import hashlib
import hmac
import re
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from setuhaul.api import AppDeps, create_app, create_app_from_env
from setuhaul.auth import sessions
from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from setuhaul.demo_ui import build_demo_app
from tests.auth_helpers import GURUGRAM, JAIPUR, World, add_user, build_world, sign_in

BOTH = frozenset({"ADMIN", "WAREHOUSE_AUTHORITY"})
ADMIN = frozenset({"ADMIN"})

# Open to anyone: no data, or their own credential check.
PUBLIC = {
    ("GET", "/health"), ("GET", "/ready"), ("GET", "/auth/login"), ("GET", "/auth/callback"), ("POST", "/auth/logout"),
    ("GET", "/auth/me"), ("POST", "/auth/local-admin"), ("GET", "/demo/login"),
    ("GET", "/openapi.json"), ("GET", "/docs"), ("GET", "/redoc"), ("GET", "/docs/oauth2-redirect"),
}
# Machine channels: each has its own signature or secret check and must refuse an unauthenticated call.
EXEMPT_OWN_AUTH = {
    ("POST", "/v1/driver/messages"), ("POST", "/v1/gps/pings"), ("POST", "/v1/driver/location"),
    ("GET", "/v1/driver/status"), ("POST", "/v1/telegram/webhook"),
}
# Everything else needs a web session with one of these roles.
ROLE_GUARDED: dict[tuple[str, str], frozenset[str]] = {
    ("GET", "/v1/coordinator/cases"): BOTH,
    ("GET", "/v1/coordinator/cases/{case_id}"): BOTH,
    ("POST", "/v1/coordinator/cases/{case_id}/claim"): BOTH,
    ("POST", "/v1/coordinator/cases/{case_id}/resolve"): BOTH,
    ("POST", "/v1/coordinator/cases/{case_id}/reopen"): BOTH,
    ("GET", "/v1/warehouse/partner-requests"): BOTH,
    ("GET", "/v1/warehouse/driver-chats"): BOTH,
    ("POST", "/v1/drivers"): BOTH,
    ("GET", "/v1/drivers/pending"): ADMIN,
    ("POST", "/v1/drivers/{driver_id}/approve"): ADMIN,
    ("POST", "/v1/drivers/{driver_id}/reject"): ADMIN,
    ("POST", "/v1/projects"): BOTH,
    ("GET", "/v1/projects"): BOTH,
    ("POST", "/v1/projects/{project_id}/allocate"): BOTH,
    ("POST", "/v1/projects/{project_id}/release"): BOTH,
}
# The whole demo console (page and API) is one guarded family, so a new demo route is covered the day it is added.
DEMO_ROLES = BOTH


def classify(method: str, path: str) -> tuple[str, frozenset[str]]:
    # One lookup; anything not listed is UNCLASSIFIED, which fails the suite.
    if (method, path) in PUBLIC:
        return "PUBLIC", frozenset()
    if (method, path) in EXEMPT_OWN_AUTH:
        return "EXEMPT", frozenset()
    if (method, path) in ROLE_GUARDED:
        return "GUARDED", ROLE_GUARDED[(method, path)]
    if path == "/demo" or path.startswith("/demo/"):
        return "GUARDED", DEMO_ROLES
    return "UNCLASSIFIED", frozenset()


def _flatten(routes, prefix: str = ""):  # type: ignore[no-untyped-def]
    # Included routers are mounted as one object; walk into them so every real route is seen with its full path.
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _flatten(inner.routes, prefix + route.include_context.prefix)
        else:
            yield prefix, route


def routes_of(app: FastAPI) -> list[tuple[str, str]]:
    # Discovered at run time: every method of every route the app really serves.
    found = []
    for prefix, route in _flatten(app.routes):
        methods = getattr(route, "methods", None) or {"GET"}
        for method in sorted(methods - {"HEAD", "OPTIONS"}):
            found.append((method, prefix + getattr(route, "path", "")))
    return found


def concrete(path: str) -> str:
    # Fill every path parameter with a placeholder id.
    return re.sub(r"\{[^}]+\}", "X1", path)


def call(client: TestClient, method: str, path: str, **kw):  # type: ignore[no-untyped-def]
    return client.request(method, concrete(path), json={} if method == "POST" else None, **kw)


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    # The widest app: production routes plus the demo console, with users of each kind.
    app, deps = build_demo_app(tmp_path_factory.mktemp("matrix"))
    deps.telegram_secret = b"t" * 20
    conn = open_connection(path=deps.db_path)
    clock = deps.clock
    users = {
        "admin": add_user(conn, clock, "boss@x.com", "ADMIN"),  # type: ignore[arg-type]
        "wh": add_user(conn, clock, "wha@x.com", "WAREHOUSE_AUTHORITY", (JAIPUR,)),  # type: ignore[arg-type]
        "wh_none": add_user(conn, clock, "whn@x.com", "WAREHOUSE_AUTHORITY", ()),  # type: ignore[arg-type]
    }
    clients = {}
    for name, user in users.items():
        c = TestClient(app, base_url="http://localhost", follow_redirects=False)
        sign_in(c, conn, user, clock)  # type: ignore[arg-type]
        clients[name] = c
    conn.close()
    return app, deps, clients


def test_every_route_is_classified(demo) -> None:  # type: ignore[no-untyped-def]
    app, _, _ = demo
    unknown = [r for r in routes_of(app) if classify(*r)[0] == "UNCLASSIFIED"]
    assert unknown == [], f"routes with no entry in the role matrix: {unknown}"


def test_the_table_has_no_stale_entries(demo) -> None:  # type: ignore[no-untyped-def]
    app, _, _ = demo
    served = set(routes_of(app))
    listed = (set(EXEMPT_OWN_AUTH) | set(ROLE_GUARDED)) - served
    assert listed == set(), f"matrix entries for routes that no longer exist: {listed}"


GUARDED_ROUTES = [r for r in routes_of(build_demo_app(Path(tempfile.mkdtemp()))[0]) if classify(*r)[0] == "GUARDED"]
EXEMPT_ROUTES = sorted(EXEMPT_OWN_AUTH)


@pytest.mark.parametrize(("method", "path"), GUARDED_ROUTES)
def test_guarded_route_enforces_its_roles(demo, method: str, path: str) -> None:  # type: ignore[no-untyped-def]
    _, _, clients = demo
    roles = classify(method, path)[1]
    anonymous = TestClient(demo[0], base_url="http://localhost", follow_redirects=False)
    # Anonymous: 401, whatever else is wrong with the request.
    assert call(anonymous, method, path).status_code == 401
    # A warehouse authority with no facility is refused everywhere (and, on admin-only routes, so is one with a facility).
    assert call(clients["wh_none"], method, path).status_code == 403
    if "WAREHOUSE_AUTHORITY" not in roles:
        assert call(clients["wh"], method, path).status_code == 403
    # Each allowed role gets past the guard (the route itself may still say 404 or 422 for the dummy ids).
    for name, role in (("admin", "ADMIN"), ("wh", "WAREHOUSE_AUTHORITY")):
        if role in roles:
            assert call(clients[name], method, path).status_code not in (401, 403), (name, method, path)


@pytest.mark.parametrize(("method", "path"), EXEMPT_ROUTES)
def test_exempt_route_refuses_unauthenticated_calls(demo, method: str, path: str) -> None:  # type: ignore[no-untyped-def]
    app, _, _ = demo
    client = TestClient(app, base_url="http://localhost", follow_redirects=False)
    query = {"phone": "+919999999999", "ts": 0} if path == "/v1/driver/status" else None
    r = client.request(method, path, params=query, content=b"{}" if method == "POST" else None)
    assert r.status_code in (401, 404, 503), (method, path, r.status_code)


def test_no_route_serves_board_data_without_web_role(demo) -> None:  # type: ignore[no-untyped-def]
    app, deps, _ = demo
    client = TestClient(app, base_url="http://localhost", follow_redirects=False)
    body = b"{}"
    # A Telegram secret and a valid HMAC channel signature are NOT web roles.
    headers = {"x-telegram-bot-api-secret-token": deps.telegram_secret.decode(),
               "x-signature": hmac.new(deps.webhook_secret, body, hashlib.sha256).hexdigest(),
               "authorization": "Bearer tok-asha"}
    for method, path in GUARDED_ROUTES:
        if path.startswith("/v1/coordinator"):
            continue  # the bearer fallback is exercised on its own in test_api.py
        r = client.request(method, concrete(path), content=body, headers=headers)
        assert r.status_code == 401, (method, path, r.status_code)
    assert client.get("/demo/api/board", headers=headers).status_code == 401
    assert client.get("/demo", headers=headers).status_code == 401


def test_demo_login_is_loopback_only_and_absent_from_production_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, deps = build_demo_app(tmp_path / "d")
    # A loopback client gets a session for the seeded demo user and can then use the console.
    local = TestClient(app, client=("127.0.0.1", 5000), base_url="http://127.0.0.1", follow_redirects=False)
    r = local.get("/demo/login")
    assert r.status_code == 302 and r.headers["location"] == "/demo"
    assert sessions.COOKIE_NAME in r.cookies
    assert local.get("/demo/api/config").status_code == 200
    # Anyone else (including the test client's own host name) is refused and gets no cookie.
    for host in ("203.0.113.9", "testclient", "10.0.0.5"):
        remote = TestClient(app, client=(host, 5000), follow_redirects=False)
        refused = remote.get("/demo/login")
        assert refused.status_code == 403 and sessions.COOKIE_NAME not in refused.cookies
    # The production factories never register it.
    plain = create_app(AppDeps(db_path=deps.db_path, redis=deps.redis, webhook_secret=b"w", coordinator_tokens={},
                               fallback_path=tmp_path / "f.jsonl"))
    assert TestClient(plain, client=("127.0.0.1", 5000)).get("/demo/login").status_code == 404
    monkeypatch.setenv("SETUHAUL_REDIS_URL", "fakeredis://")
    monkeypatch.setenv("SETUHAUL_DB_PATH", str(deps.db_path))
    prod = create_app_from_env()
    assert ("GET", "/demo/login") not in routes_of(prod)
    assert TestClient(prod, client=("127.0.0.1", 5000)).get("/demo/login").status_code == 404


# ---- behaviour behind the guards ---------------------------------------------------------------------------
@pytest.fixture()
def world(tmp_path: Path, clock: FakeClock) -> World:
    return build_world(tmp_path, clock)


def test_warehouse_sees_chats_only_for_project_drivers(world: World) -> None:
    a = world.client("wh_a").get("/v1/warehouse/driver-chats")
    assert a.status_code == 200
    drivers = {d["driver_id"]: d for d in a.json()["drivers"]}
    # Only the driver on wh_a's own project, with name and recent messages (role, text, timestamp).
    assert set(drivers) == {"DRV001"}
    item = drivers["DRV001"]
    assert item["driver_name"]
    texts = [m["text"] for m in item["messages"]]
    assert any("hello from DRV001" in t for t in texts)
    assert {"role", "text", "timestamp"} <= set(item["messages"][0])
    b = world.client("wh_b").get("/v1/warehouse/driver-chats").json()["drivers"]
    assert {d["driver_id"] for d in b} == {"DRV006"}
    # Limits are capped, never trusted.
    assert world.client("wh_a").get("/v1/warehouse/driver-chats", params={"limit": 100000, "messages": 100000}).status_code == 200


def test_admin_sees_all(world: World) -> None:
    admin = world.client("admin")
    chats = {d["driver_id"] for d in admin.get("/v1/warehouse/driver-chats").json()["drivers"]}
    assert {"DRV001", "DRV006"} <= chats
    refs = {p["request_ref"] for p in admin.get("/v1/warehouse/partner-requests").json()["requests"]}
    assert refs == {f"PR-T-{JAIPUR}", f"PR-T-{GURUGRAM}"}
    listed = {c["case_id"] for c in admin.get("/v1/coordinator/cases").json()["cases"]}
    # Admin also sees the case that names no load.
    assert listed == set(world.cases.values())
    assert admin.get(f"/v1/coordinator/cases/{world.cases['NONE']}").status_code == 200


def test_partner_requests_are_scoped_to_the_users_facilities(world: World) -> None:
    got = world.client("wh_a").get("/v1/warehouse/partner-requests").json()["requests"]
    assert [p["request_ref"] for p in got] == [f"PR-T-{JAIPUR}"]
    assert got[0]["facility_id"] == JAIPUR and got[0]["status"] == "PENDING_PARTNER"
    assert world.client("wh_none").get("/v1/warehouse/partner-requests").status_code == 403
    assert world.client().get("/v1/warehouse/partner-requests", headers={"authorization": "Bearer tok-asha"}).status_code == 401


def test_warehouse_cannot_read_other_facility_cases(world: World) -> None:
    a, b = world.client("wh_a"), world.client("wh_b")
    mine, theirs, none = world.cases[JAIPUR], world.cases[GURUGRAM], world.cases["NONE"]
    assert a.get(f"/v1/coordinator/cases/{mine}").status_code == 200
    # Out of scope looks like it does not exist, for reading and for every change.
    assert a.get(f"/v1/coordinator/cases/{theirs}").status_code == 404
    assert a.get(f"/v1/coordinator/cases/{none}").status_code == 404
    assert a.post(f"/v1/coordinator/cases/{theirs}/claim").status_code == 404
    assert a.post(f"/v1/coordinator/cases/{theirs}/resolve", json={"resolution_code": "ADVISED_DRIVER", "note": "x"}).status_code == 404
    assert a.post(f"/v1/coordinator/cases/{theirs}/reopen").status_code == 404
    assert [c["case_id"] for c in a.get("/v1/coordinator/cases").json()["cases"]] == [mine]
    assert [c["case_id"] for c in b.get("/v1/coordinator/cases").json()["cases"]] == [theirs]
    # Nothing was changed by the refused calls.
    conn = open_connection(path=world.path, readonly=True)
    assert conn.execute("select status from escalation_cases where case_id=?", (theirs,)).fetchone()[0] == "OPEN"
    conn.close()


def test_session_user_claims_and_resolves_under_their_name(world: World) -> None:
    a = world.client("wh_a")
    cid = world.cases[JAIPUR]
    assert a.post(f"/v1/coordinator/cases/{cid}/claim").json()["status"] == "CLAIMED"
    r = a.post(f"/v1/coordinator/cases/{cid}/resolve", json={"resolution_code": "ADVISED_DRIVER", "note": "called"})
    assert r.status_code == 200 and r.json()["status"] == "RESOLVED"
    conn = open_connection(path=world.path, readonly=True)
    assert conn.execute("select claimed_by from escalation_cases where case_id=?", (cid,)).fetchone()[0] == "Wha"
    conn.close()


def test_legacy_bearer_token_still_works_and_unauthenticated_is_401(world: World) -> None:
    anon = world.client()
    assert anon.get("/v1/coordinator/cases").status_code == 401
    bearer = {"authorization": "Bearer tok-asha"}
    assert {c["case_id"] for c in anon.get("/v1/coordinator/cases", headers=bearer).json()["cases"]} == set(world.cases.values())
    claimed = anon.post(f"/v1/coordinator/cases/{world.cases[GURUGRAM]}/claim", headers=bearer).json()
    assert claimed["status"] == "CLAIMED"
    conn = open_connection(path=world.path, readonly=True)
    assert conn.execute("select claimed_by from escalation_cases where case_id=?", (world.cases[GURUGRAM],)).fetchone()[0] == "Asha Verma"
    conn.close()
