"""Server-rendered admin and warehouse screens: routing by role, scoping, escaping, headers and assets."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import re
import tomllib
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import fakeredis
import pytest
from fastapi.testclient import TestClient

from setuhaul.api import AppDeps, create_app
from setuhaul.auth import audit, onboarding, users
from setuhaul.auth.google import FakeGoogleVerifier, GoogleIdentity
from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection
from tests.auth_helpers import GURUGRAM, JAIPUR, World, build_world

ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "src" / "setuhaul" / "auth" / "assets"
ASSETS = ("tokens.css", "app.css", "common.js", "tabs.js", "admin.js", "warehouse.js", "model.js", "settings.js", "password.js")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'self'")
XSS_NAME = "<script>alert(1)</script>"
XSS_DISPLAY = '"><img src=x onerror=alert(1)>'


@pytest.fixture()
def world(tmp_path: Path, clock: FakeClock) -> World:
    return build_world(tmp_path, clock)


def _add_driver(world: World, who: str = "wh_a", name: str = "Ravi Kumar", phone: str = "+91 90000 10099",
                licence: str = "SECRET-LIC-99") -> str:
    # A pending driver added through the data layer by the named user.
    conn = open_connection(path=world.path)
    try:
        veh = conn.execute("select vehicle_id, carrier_id from vehicles where active_flag=1 order by vehicle_id").fetchone()
        return onboarding.add_driver(conn, actor=world.users[who], name=name, phone=phone, licence_number=licence,
                                     licence_expiry=world.clock.now().date() + timedelta(days=400),
                                     carrier_id=veh["carrier_id"], vehicle_id=veh["vehicle_id"], clock=world.clock)
    finally:
        conn.close()


def test_root_redirects_by_role(world: World) -> None:
    # Google is offered only when it is configured.
    assert "/auth/login" not in world.client().get("/").text
    world.deps.google, world.deps.session_secret = FakeGoogleVerifier(), b"s" * 32
    anon = world.client().get("/")
    assert anon.status_code == 200 and "/auth/login" in anon.text and "Sign in with Google" in anon.text
    # The backup form is absent unless the feature is on.
    assert "/auth/local-admin" not in anon.text
    world.deps.local_admin_enabled = True
    assert "/auth/local-admin" in world.client().get("/").text
    world.deps.local_admin_enabled = False
    admin = world.client("admin").get("/")
    assert admin.status_code == 303 and admin.headers["location"] == "/admin"
    wh = world.client("wh_a").get("/")
    assert wh.status_code == 303 and wh.headers["location"] == "/warehouse"


def test_admin_page_shows_pending_driver_and_audit_rows(world: World) -> None:
    did = _add_driver(world)
    r = world.client("admin").get("/admin")
    assert r.status_code == 200
    # Users, pending drivers (masked phone, name) and the audit log with the driver-added row.
    assert "wha@x.com" in r.text and "Ravi Kumar" in r.text and did in r.text
    assert "0099" in r.text and "+919000010099" not in r.text and "SECRET-LIC-99" not in r.text
    assert "DRIVER_ADDED" in r.text and "Audit log" in r.text and "Pending drivers" in r.text
    # The audit filters apply on the server.
    only = world.client("admin").get("/admin", params={"action": "NO_SUCH_ACTION"})
    assert "DRIVER_ADDED" not in only.text.split("Audit log", 1)[1]
    # A bad "since" is explained, not a server error.
    assert world.client("admin").get("/admin", params={"since": "yesterday"}).status_code == 200


def test_admin_page_has_one_panel_per_tab(world: World) -> None:
    text = world.client("admin").get("/admin").text
    keys = ["users", "pending", "decided", "deleted", "audit", "model", "config"]
    assert re.findall(r'data-tab="(\w+)"', text) == keys
    assert re.findall(r'<section id="\w+" data-panel="(\w+)"', text) == keys
    assert text.index("/app/assets/tabs.js") < text.index("/app/assets/admin.js")
    assert 'action="/admin#audit"' in text


def test_admin_page_forbidden_for_warehouse_and_redirects_anonymous(world: World) -> None:
    anon = world.client().get("/admin")
    assert anon.status_code == 303 and anon.headers["location"] == "/"
    wrong = world.client("wh_a").get("/admin")
    assert wrong.status_code == 403 and "Audit log" not in wrong.text
    # The refusal is audited like a JSON refusal.
    conn = open_connection(path=world.path, readonly=True)
    assert [r["target_id"] for r in audit.query(conn, action="ACCESS_DENIED")] == ["/admin"]
    conn.close()
    # No facilities: the same guard refuses the warehouse page.
    assert world.client("wh_none").get("/warehouse").status_code == 403
    assert world.client().get("/warehouse").headers["location"] == "/"


def test_warehouse_page_has_board_and_no_audit_section(world: World) -> None:
    r = world.client("wh_a").get("/warehouse")
    assert r.status_code == 200
    for heading in ("Dock board", "Operations", "Projects", "Drivers"):
        assert heading in r.text
    assert "Audit log" not in r.text and "Invite" not in r.text
    # Board, open case with its summary, partner request and the project driver's chat, all for Jaipur.
    assert "<table" in r.text and "D1" in r.text
    assert "needs help" in r.text and f"PR-T-{JAIPUR}" in r.text and "hello from DRV001" in r.text
    assert f"PR-T-{GURUGRAM}" not in r.text and "hello from DRV006" not in r.text
    # The note about admin-created projects.
    assert "You see messages about loads going to your facilities." in r.text


def _facilities(page: str) -> list[str]:
    # The facility buttons above the board.
    chooser = page.split('<nav class="facilities"', 1)[1].split("</nav>", 1)[0]
    return re.findall(r'href="/warehouse\?facility=([^"#]+)#board"', chooser)


def test_warehouse_page_only_lists_own_facility_in_selector_and_other_facility_board_is_404(world: World) -> None:
    c = world.client("wh_a")
    r = c.get("/warehouse")
    assert _facilities(r.text) == [JAIPUR]
    assert c.get("/warehouse", params={"facility": JAIPUR}).status_code == 200
    # Another facility, and one that does not exist, look the same.
    assert c.get("/warehouse", params={"facility": GURUGRAM}).status_code == 404
    assert c.get("/warehouse", params={"facility": "NOPE"}).status_code == 404
    # An admin sees every facility.
    admin = world.client("admin").get("/warehouse")
    assert {JAIPUR, GURUGRAM} <= set(_facilities(admin.text))
    assert world.client("admin").get("/warehouse", params={"facility": GURUGRAM}).status_code == 200


def test_warehouse_page_driver_list_masks_phone_and_hides_licence_number(world: World) -> None:
    did = _add_driver(world)
    conn = open_connection(path=world.path, readonly=True)
    row = conn.execute("select phone, licence_number from drivers where driver_id='DRV001'").fetchone()
    conn.close()
    text = world.client("wh_a").get("/warehouse").text
    assert did in text and "DRV001" in text
    assert row["phone"] not in text and "+919000010099" not in text and "SECRET-LIC-99" not in text and row["licence_number"] not in text
    assert "0099" in text and "•" in text


def test_warehouse_can_revoke_telegram_binding_and_it_is_audited(world: World) -> None:
    did = _add_driver(world)
    conn = open_connection(path=world.path)
    conn.execute("insert into telegram_identities(telegram_user_id, chat_id, driver_id, bound_at) values ('777','777',?,'x')", (did,))
    conn.commit()
    conn.close()
    c = world.client("wh_a")
    page = c.get("/warehouse").text
    # A Revoke button for the bound driver they onboarded, carrying the API path the script calls.
    assert f"/v1/warehouse/drivers/{did}/telegram/revoke" in page and ">Linked<" in page
    assert c.post(f"/v1/warehouse/drivers/{did}/telegram/revoke", json={}).status_code == 200
    assert f"/v1/warehouse/drivers/{did}/telegram/revoke" not in c.get("/warehouse").text
    conn = open_connection(path=world.path, readonly=True)
    assert [r["target_id"] for r in audit.query(conn, action="TELEGRAM_REVOKED", outcome="OK")] == [did]
    conn.close()


def test_pages_escape_user_text(world: World) -> None:
    _add_driver(world, name=XSS_NAME)
    conn = open_connection(path=world.path)
    conn.execute("update users set display_name=? where email='wha@x.com'", (XSS_DISPLAY,))
    audit.record(conn, actor_user_id=None, actor_email="evil@x.com", action="X", target_type=None, target_id=None,
                 outcome="OK", ip=None, detail={"note": "<b>bold</b>", "token": "SHOULD-NOT-SHOW"}, clock=world.clock)
    conn.execute("update chat_messages set message_text=? where chat_message_id=?", ("<i>hi</i>", f"MSG-T-{JAIPUR}"))
    conn.execute("update escalation_cases set claimed_by='web:wha@x.com', status='CLAIMED' where case_id=?", (f"ESC-T-{JAIPUR}",))
    conn.commit()
    conn.close()
    admin = world.client("admin").get("/admin").text
    warehouse = world.client("admin").get("/warehouse").text
    for body in (admin, warehouse):
        assert XSS_NAME not in body and XSS_DISPLAY not in body and "<b>bold</b>" not in body and "<i>hi</i>" not in body
        assert "onerror=alert(1)>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in admin
    assert "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;" in admin
    assert "&lt;b&gt;bold&lt;/b&gt;" in admin and "SHOULD-NOT-SHOW" not in admin
    assert "&lt;i&gt;hi&lt;/i&gt;" in warehouse and "&quot;&gt;&lt;img" in warehouse


def _all_pages(world: World) -> list:  # type: ignore[type-arg]
    return [world.client().get("/"), world.client("admin").get("/admin"), world.client("wh_a").get("/warehouse"),
            world.client("wh_a").get("/admin"), world.client("admin").get("/warehouse"),
            world.client("wh_a").get("/warehouse", params={"facility": GURUGRAM}), world.client().get("/admin"),
            world.client("admin").get("/")]


def test_security_headers_on_every_page(world: World) -> None:
    for r in _all_pages(world):
        assert r.headers["content-security-policy"] == CSP, r.url
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["cache-control"] == "no-store"


def test_no_inline_script_or_style_in_pages(world: World) -> None:
    _add_driver(world)
    for r in _all_pages(world):
        if r.status_code in (303,):
            continue
        assert re.search(r"<script(?![^>]*\bsrc=)", r.text, re.IGNORECASE) is None, r.url
        assert re.search(r"\sstyle\s*=", r.text, re.IGNORECASE) is None, r.url
        assert re.search(r"<style", r.text, re.IGNORECASE) is None and re.search(r"\son[a-z]+\s*=", r.text, re.IGNORECASE) is None, r.url


def test_js_assets_do_not_use_innerhtml() -> None:
    for name in ASSETS:
        text = (ASSET_DIR / name).read_text(encoding="utf-8")
        assert text.strip()
        for banned in ("innerHTML", "outerHTML", "document.write", "insertAdjacentHTML", "eval(", "new Function"):
            assert banned not in text, (name, banned)


def test_assets_whitelist_and_no_traversal(world: World) -> None:
    c = world.client()
    types = {"tokens.css": "text/css", "app.css": "text/css", "common.js": "text/javascript", "admin.js": "text/javascript", "warehouse.js": "text/javascript",
             "tabs.js": "text/javascript", "model.js": "text/javascript",
             "settings.js": "text/javascript"}
    for name, ctype in types.items():
        r = c.get(f"/app/assets/{name}")
        assert r.status_code == 200 and r.headers["content-type"].startswith(ctype)
        assert r.headers["cache-control"] == "no-cache" and r.headers["x-content-type-options"] == "nosniff"
        assert r.content == (ASSET_DIR / name).read_bytes()
    for bad in ("../../etc/passwd", "%2e%2e/%2e%2e/etc/passwd", "..%2f..%2fetc%2fpasswd", "secret.js", "app.css.map", "", "pages.py",
                "..\\pages.py", "app.css%00.js"):
        assert c.get(f"/app/assets/{bad}").status_code == 404, bad


def test_form_encoded_post_to_admin_api_is_rejected(world: World) -> None:
    c = world.client("admin")
    # A cross-site form can only send form encodings; the JSON-only APIs refuse them even with a valid cookie.
    r = c.post("/v1/admin/users", data={"email": "new@x.com", "role": "ADMIN", "display_name": "N"})
    assert r.status_code in (415, 422)
    r = c.post("/v1/projects", data={"customer_name": "a"}, headers={"content-type": "text/plain"})
    assert r.status_code in (415, 422)
    conn = open_connection(path=world.path, readonly=True)
    assert users.find_by_email(conn, "new@x.com") is None
    conn.close()


def test_asset_files_are_in_package_data() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = data["tool"]["setuptools"]["package-data"]["setuhaul"]
    assert "auth/assets/*" in patterns or all(f"auth/assets/{n}" in patterns for n in ASSETS)
    for name in ASSETS:
        assert (ASSET_DIR / name).is_file()


def test_end_to_end_admin_to_warehouse_flow(tmp_path: Path, clock: FakeClock) -> None:
    path = make_working_db(tmp_path / "e2e.sqlite3")
    conn = open_connection(path=path)
    users.bootstrap_admin(conn, "boss@x.com", clock)
    conn.close()
    google = FakeGoogleVerifier(GoogleIdentity("boss@x.com", True, "Boss"))
    deps = AppDeps(db_path=path, redis=fakeredis.FakeRedis(decode_responses=True), webhook_secret=b"w" * 16, coordinator_tokens={},
                   fallback_path=tmp_path / "f.jsonl", clock=clock, google=google, session_secret=b"s" * 32)
    app = create_app(deps)

    def login(email: str) -> TestClient:
        # The real Google code flow against the fake verifier.
        google.identity = GoogleIdentity(email, True, email)
        client = TestClient(app, base_url="http://localhost", follow_redirects=False)
        state = parse_qs(urlparse(client.get("/auth/login").headers["location"]).query)["state"][0]
        assert client.get("/auth/callback", params={"code": "c", "state": state}).status_code == 302
        return client

    admin = login("boss@x.com")
    assert admin.get("/").headers["location"] == "/admin"
    # Invite a warehouse authority for Jaipur (what the admin screen's form posts).
    assert admin.post("/v1/admin/users", json={"email": "wh@x.com", "role": "WAREHOUSE_AUTHORITY", "display_name": "Wh",
                                              "facilities": [JAIPUR]}).status_code == 201
    wh = login("wh@x.com")
    assert wh.get("/").headers["location"] == "/warehouse"
    # The authority adds a driver from the screen's selects.
    page = wh.get("/warehouse").text
    veh = re.search(r'<option value="(VEH\d+)" data-carrier="(CAR\d+)"', page)
    assert veh is not None
    added = wh.post("/v1/drivers", json={"name": "Meena", "phone": "+91 90000 10077", "licence_number": "LIC-E2E-1",
                                         "licence_expiry": (clock.now().date() + timedelta(days=300)).isoformat(),
                                         "carrier_id": veh.group(2), "vehicle_id": veh.group(1)})
    assert added.status_code == 201
    driver_id = added.json()["driver_id"]
    assert driver_id in admin.get("/admin").text
    # Admin approves; the authority creates a project and allocates the approved driver.
    assert admin.post(f"/v1/drivers/{driver_id}/approve", json={}).status_code == 200
    project = wh.post("/v1/projects", json={"customer_name": "Acme", "contract_ref": "E2E-1", "facility_id": JAIPUR,
                                            "starts_on": clock.now().date().isoformat()})
    assert project.status_code == 201
    pid = project.json()["project_id"]
    assert pid in wh.get("/warehouse").text
    assert wh.post(f"/v1/projects/{pid}/allocate", json={"driver_id": driver_id}).status_code == 200
    # The project now lists the driver with a release control.
    assert driver_id in wh.get("/warehouse").text
    # A binding exists and is revoked through the screen's API.
    conn = open_connection(path=path)
    conn.execute("insert into telegram_identities(telegram_user_id, chat_id, driver_id, bound_at) values ('9','9',?,'x')", (driver_id,))
    conn.commit()
    conn.close()
    assert wh.post(f"/v1/warehouse/drivers/{driver_id}/telegram/revoke", json={}).status_code == 200
    assert wh.post(f"/v1/projects/{pid}/release", json={"driver_id": driver_id}).status_code == 200
    conn = open_connection(path=path, readonly=True)
    actions = {r["action"] for r in audit.query(conn, limit=500)}
    conn.close()
    assert {"DRIVER_ADDED", "TELEGRAM_REVOKED", "LOGIN"} <= actions
