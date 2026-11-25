"""A refused Google sign-in sends a browser back to the login page with a plain message, never raw JSON."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from pathlib import Path

import pytest

from setuhaul.auth import sessions
from setuhaul.auth.google import FakeGoogleVerifier, GoogleIdentity
from setuhaul.clock import FakeClock
from tests.test_auth_google import _make, _rows, _start

BROWSER = {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


@pytest.mark.parametrize("identity,error,key,words", [
    (GoogleIdentity("stranger@x.com", True, "S"), None, "not_invited", "does not have access"),
    (GoogleIdentity("ok@x.com", False, "Ok"), None, "unverified", "has not verified"),
    (None, "BAD_TOKEN", "expired", "could not be verified"),
])
def test_browser_is_sent_back_to_the_login_page(tmp_path: Path, clock: FakeClock, identity, error, key, words) -> None:  # type: ignore[no-untyped-def]
    path, client = _make(tmp_path, clock, FakeGoogleVerifier(identity, error))
    state, _ = _start(client)
    r = client.get("/auth/callback", params={"code": "c", "state": state}, headers=BROWSER)
    assert r.status_code == 303 and r.headers["location"] == f"/?signin={key}"
    # No session; the single-use state cookie is still cleared; the refusal is still audited.
    assert sessions.COOKIE_NAME not in r.cookies and "setuhaul_oauth=" in r.headers["set-cookie"]
    assert len(_rows(path, "LOGIN_REFUSED")) == 1
    page = client.get(r.headers["location"]).text
    assert words in page
    # Nothing about the account is put in the address or on the page.
    assert "stranger@x.com" not in page and "stranger" not in r.headers["location"]


def test_cancelling_at_google_returns_to_the_login_page(tmp_path: Path, clock: FakeClock) -> None:
    _, client = _make(tmp_path, clock, FakeGoogleVerifier())
    r = client.get("/auth/callback", params={"error": "access_denied"}, headers=BROWSER)
    assert r.status_code == 303 and r.headers["location"] == "/?signin=cancelled"


def test_unconfigured_google_returns_to_the_login_page(tmp_path: Path, clock: FakeClock) -> None:
    _, client = _make(tmp_path, clock, None)  # type: ignore[arg-type]
    r = client.get("/auth/login", headers=BROWSER)
    assert r.status_code == 303 and r.headers["location"] == "/?signin=unavailable"


def test_api_callers_still_get_the_reason_code(tmp_path: Path, clock: FakeClock) -> None:
    _, client = _make(tmp_path, clock, FakeGoogleVerifier(GoogleIdentity("stranger@x.com", True, "S")))
    state, _ = _start(client)
    r = client.get("/auth/callback", params={"code": "c", "state": state})
    assert r.status_code == 403 and r.json()["reason_code"] == "NOT_INVITED"


def test_only_known_messages_are_ever_shown(tmp_path: Path, clock: FakeClock) -> None:
    _, client = _make(tmp_path, clock, FakeGoogleVerifier())
    page = client.get("/", params={"signin": "<script>alert(1)</script>"}).text
    assert "alert(1)" not in page and 'role="alert"></p>' in page
