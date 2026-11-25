"""The local demo uses the real sign-in: a login page with Google and the backup admin password, no automatic sign-in."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from setuhaul.auth.google import FakeGoogleVerifier, GoogleIdentity
from setuhaul.demo_ui import DemoSignIn, build_demo_app, sign_in_from_env

PASSWORD = "correct horse battery"
BOSS = "boss@example.com"


def _client(app: Any) -> TestClient:
    return TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000), follow_redirects=False)


def _password_app(tmp_path: Path) -> Any:
    return build_demo_app(tmp_path, auto_login=False,
                          sign_in=DemoSignIn(local_admin=True, admin_email=BOSS, admin_password=PASSWORD))[0]


def test_signed_out_visitor_is_sent_to_the_login_page(tmp_path: Path) -> None:
    client = _client(_password_app(tmp_path))
    r = client.get("/demo")
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_nothing_configured_says_so_and_offers_no_dead_button(tmp_path: Path) -> None:
    client = _client(build_demo_app(tmp_path, auto_login=False)[0])
    page = client.get("/").text
    assert "No sign-in is configured" in page
    assert "/auth/login" not in page and "/auth/local-admin" not in page
    # Without --auto-login there is no way in that skips authentication.
    assert client.get("/demo/login").status_code == 404


def test_backup_password_signs_in_and_survives_a_reset(tmp_path: Path) -> None:
    app = _password_app(tmp_path)
    client = _client(app)
    page = client.get("/").text
    assert "/auth/local-admin" in page and "/auth/login" not in page
    assert client.post("/auth/local-admin", json={"email": BOSS, "password": "wrong password here"}).status_code == 401
    assert client.post("/auth/local-admin", json={"email": BOSS, "password": PASSWORD}).status_code == 200
    assert client.get("/").headers["location"] == "/admin"
    assert client.get("/demo").status_code == 200
    assert client.post("/demo/api/reset", json={}).status_code == 200
    again = _client(app)
    assert again.post("/auth/local-admin", json={"email": BOSS, "password": PASSWORD}).status_code == 200


def test_google_button_appears_when_google_is_configured(tmp_path: Path) -> None:
    google = FakeGoogleVerifier(GoogleIdentity(email=BOSS, email_verified=True, name="Boss"))
    app = build_demo_app(tmp_path, auto_login=False,
                         sign_in=DemoSignIn(google=google, session_secret=b"s" * 32, admin_email=BOSS))[0]
    client = _client(app)
    assert 'href="/auth/login"' in client.get("/").text
    assert client.get("/auth/login").status_code in (302, 303, 307)


def test_settings_come_from_the_environment() -> None:
    nothing = sign_in_from_env({})
    assert nothing.google is None and nothing.local_admin is False and nothing.admin_email == ""
    got = sign_in_from_env({"SETUHAUL_GOOGLE_CLIENT_ID": "id", "SETUHAUL_GOOGLE_CLIENT_SECRET": "secret",
                            "SETUHAUL_BOOTSTRAP_ADMIN_EMAIL": BOSS, "SETUHAUL_LOCAL_ADMIN": "1"})
    assert got.google is not None and got.local_admin is True and got.admin_email == BOSS
    # A missing or short signing secret is replaced for this run, so Google sign-in still works locally.
    assert len(got.session_secret) >= 32
