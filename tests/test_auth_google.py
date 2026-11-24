"""Google sign-in routes: invite-only code flow with PKCE, signed single-use state, audited outcomes."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient

from setuhaul.api import AppDeps, create_app
from setuhaul.auth import audit, sessions, users
from setuhaul.auth.google import FakeGoogleVerifier, GoogleIdentity, RealGoogleVerifier
from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection
from setuhaul.identity import AuthError

SECRET = b"s" * 32
CLIENT_ID = "cid.apps.googleusercontent.com"


def _make(tmp_path: Path, clock: FakeClock, google: FakeGoogleVerifier, base: str = "http://localhost"):  # type: ignore[no-untyped-def]
    path = make_working_db(tmp_path / "auth.sqlite3")
    conn = open_connection(path=path)
    admin = users.bootstrap_admin(conn, "boss@x.com", clock)
    users.invite(conn, actor=admin, email="ok@x.com", role="ADMIN", display_name="Ok", clock=clock)
    conn.close()
    deps = AppDeps(db_path=path, redis=fakeredis.FakeRedis(decode_responses=True), webhook_secret=b"w",
                   coordinator_tokens={}, fallback_path=tmp_path / "f.jsonl", clock=clock, google=google,
                   session_secret=SECRET)
    return path, TestClient(create_app(deps), base_url=base, follow_redirects=False)


def _start(client: TestClient) -> tuple[str, str]:
    # Begin login; return the state in the redirect and the signed cookie.
    r = client.get("/auth/login")
    assert r.status_code == 302
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    return state, client.cookies["setuhaul_oauth"]


def _rows(path: Path, outcome: str) -> list:  # type: ignore[type-arg]
    conn = open_connection(path=path, readonly=True)
    try:
        return audit.query(conn, outcome=outcome)
    finally:
        conn.close()


def test_login_redirects_with_state_and_challenge(tmp_path: Path, clock: FakeClock) -> None:
    _, client = _make(tmp_path, clock, FakeGoogleVerifier(GoogleIdentity("ok@x.com", True, "Ok")))
    r = client.get("/auth/login")
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert r.status_code == 302 and q["state"] and q["code_challenge"] and q["nonce"]
    # HttpOnly cookie, not Secure on localhost.
    set_cookie = r.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "secure" not in set_cookie


def test_cookie_is_secure_off_localhost(tmp_path: Path, clock: FakeClock) -> None:
    _, client = _make(tmp_path, clock, FakeGoogleVerifier(), base="https://app.example.com")
    assert "secure" in client.get("/auth/login").headers["set-cookie"].lower()


def test_callback_invited_user_gets_cookie(tmp_path: Path, clock: FakeClock) -> None:
    path, client = _make(tmp_path, clock, FakeGoogleVerifier(GoogleIdentity("OK@x.com", True, "Ok")))
    state, _ = _start(client)
    r = client.get("/auth/callback", params={"code": "c", "state": state})
    assert r.status_code == 302
    assert client.get("/auth/me").json()["email"] == "ok@x.com"
    assert client.get("/auth/me").json()["role"] == "ADMIN"
    assert len(_rows(path, "LOGIN_OK")) == 1


def test_callback_uninvited_email_refused_no_user_row_no_session(tmp_path: Path, clock: FakeClock) -> None:
    path, client = _make(tmp_path, clock, FakeGoogleVerifier(GoogleIdentity("stranger@x.com", True, "S")))
    state, _ = _start(client)
    r = client.get("/auth/callback", params={"code": "c", "state": state})
    assert r.status_code == 403 and r.json()["reason_code"] == "NOT_INVITED"
    conn = open_connection(path=path, readonly=True)
    assert users.find_by_email(conn, "stranger@x.com") is None
    assert conn.execute("select count(*) from auth_sessions").fetchone()[0] == 0
    conn.close()
    assert client.get("/auth/me").status_code == 401


def test_callback_disabled_user_refused(tmp_path: Path, clock: FakeClock) -> None:
    path, client = _make(tmp_path, clock, FakeGoogleVerifier(GoogleIdentity("ok@x.com", True, "Ok")))
    conn = open_connection(path=path)
    conn.execute("update users set status='DISABLED' where email='ok@x.com'")
    conn.commit()
    conn.close()
    state, _ = _start(client)
    r = client.get("/auth/callback", params={"code": "c", "state": state})
    assert r.status_code == 403 and r.json()["reason_code"] == "DISABLED"
    assert client.get("/auth/me").status_code == 401


def test_callback_unverified_email_refused(tmp_path: Path, clock: FakeClock) -> None:
    _, client = _make(tmp_path, clock, FakeGoogleVerifier(GoogleIdentity("ok@x.com", False, "Ok")))
    state, _ = _start(client)
    r = client.get("/auth/callback", params={"code": "c", "state": state})
    assert r.status_code == 403 and r.json()["reason_code"] == "EMAIL_NOT_VERIFIED"
    assert client.get("/auth/me").status_code == 401


def test_callback_bad_token_refused(tmp_path: Path, clock: FakeClock) -> None:
    _, client = _make(tmp_path, clock, FakeGoogleVerifier(error="BAD_TOKEN"))
    state, _ = _start(client)
    assert client.get("/auth/callback", params={"code": "c", "state": state}).json()["reason_code"] == "BAD_TOKEN"


def test_callback_tampered_state_refused(tmp_path: Path, clock: FakeClock) -> None:
    path, client = _make(tmp_path, clock, FakeGoogleVerifier(GoogleIdentity("ok@x.com", True, "Ok")))
    state, cookie = _start(client)
    # State in the query does not match the signed cookie.
    assert client.get("/auth/callback", params={"code": "c", "state": "other"}).status_code == 400
    # A forged cookie with a bad signature.
    client.cookies.set("setuhaul_oauth", cookie[:-3] + "AAA")
    assert client.get("/auth/callback", params={"code": "c", "state": state}).status_code == 400
    assert client.get("/auth/me").status_code == 401
    assert len(_rows(path, "LOGIN_REFUSED")) == 2


def test_callback_replayed_state_refused(tmp_path: Path, clock: FakeClock) -> None:
    _, client = _make(tmp_path, clock, FakeGoogleVerifier(GoogleIdentity("ok@x.com", True, "Ok")))
    state, cookie = _start(client)
    assert client.get("/auth/callback", params={"code": "c", "state": state}).status_code == 302
    # Replay the very same signed cookie and state.
    client.cookies.set("setuhaul_oauth", cookie)
    assert client.get("/auth/callback", params={"code": "c", "state": state}).status_code == 400


def test_expired_state_refused(tmp_path: Path, clock: FakeClock) -> None:
    from datetime import timedelta

    _, client = _make(tmp_path, clock, FakeGoogleVerifier(GoogleIdentity("ok@x.com", True, "Ok")))
    state, _ = _start(client)
    clock.advance(timedelta(minutes=11))
    assert client.get("/auth/callback", params={"code": "c", "state": state}).status_code == 400


def test_logout_revokes(tmp_path: Path, clock: FakeClock) -> None:
    _, client = _make(tmp_path, clock, FakeGoogleVerifier(GoogleIdentity("ok@x.com", True, "Ok")))
    state, _ = _start(client)
    client.get("/auth/callback", params={"code": "c", "state": state})
    token = client.cookies[sessions.COOKIE_NAME]
    assert client.post("/auth/logout").status_code == 200
    # The old token no longer works even if a client keeps it.
    client.cookies.set(sessions.COOKIE_NAME, token)
    assert client.get("/auth/me").status_code == 401


def test_me_unauthenticated_code(tmp_path: Path, clock: FakeClock) -> None:
    _, client = _make(tmp_path, clock, FakeGoogleVerifier())
    r = client.get("/auth/me")
    assert r.status_code == 401 and r.json()["code"] == "UNAUTHENTICATED"


def test_every_outcome_is_audited(tmp_path: Path, clock: FakeClock) -> None:
    path, client = _make(tmp_path, clock, FakeGoogleVerifier(GoogleIdentity("ok@x.com", True, "Ok")))
    state, _ = _start(client)
    client.get("/auth/callback", params={"code": "c", "state": state})
    client.get("/auth/callback", params={"code": "c", "state": "bogus"})
    assert len(_rows(path, "LOGIN_OK")) == 1 and len(_rows(path, "LOGIN_REFUSED")) == 1


# ---- RealGoogleVerifier with a canned token endpoint ----

def _jwt(claims: dict) -> str:  # type: ignore[type-arg]
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"e30.{body}.sig"


def _real(clock: FakeClock, claims: dict) -> RealGoogleVerifier:  # type: ignore[type-arg]
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(parse_qs(request.content.decode()) and {k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(200, json={"id_token": _jwt(claims)})

    v = RealGoogleVerifier(CLIENT_ID, "sec", httpx.Client(transport=httpx.MockTransport(handler)), clock)
    v.seen = seen  # type: ignore[attr-defined]
    return v


def _claims(clock: FakeClock, **over: object) -> dict:  # type: ignore[type-arg]
    base = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "exp": clock.now().timestamp() + 600,
            "email": "ok@x.com", "email_verified": True, "name": "Ok", "nonce": "n1"}
    base.update(over)
    return base


def test_real_verifier_accepts_good_token(clock: FakeClock) -> None:
    v = _real(clock, _claims(clock))
    ident = v.exchange_and_verify("code", "ver", "http://localhost/auth/callback", "n1")
    assert ident == GoogleIdentity("ok@x.com", True, "Ok")
    # The PKCE verifier went to Google.
    assert v.seen["code_verifier"] == "ver"  # type: ignore[attr-defined]


@pytest.mark.parametrize("over", [
    {"aud": "someone-else"}, {"iss": "https://evil.example"}, {"exp": 1.0}, {"nonce": "other"},
    {"email": ""},
])
def test_real_verifier_rejects_wrong_audience_and_expired_token(clock: FakeClock, over: dict) -> None:  # type: ignore[type-arg]
    v = _real(clock, _claims(clock, **over))
    with pytest.raises(AuthError) as e:
        v.exchange_and_verify("code", "ver", "http://localhost/auth/callback", "n1")
    assert e.value.code == "BAD_TOKEN"


def test_real_verifier_authorize_url_has_pkce(clock: FakeClock) -> None:
    v = _real(clock, _claims(clock))
    q = parse_qs(urlparse(v.authorize_url("st", "ch", "http://localhost/auth/callback", "n1")).query)
    assert q["code_challenge_method"] == ["S256"] and q["nonce"] == ["n1"] and q["client_id"] == [CLIENT_ID]
    assert re.fullmatch(r"openid email profile", q["scope"][0])


# ---- hardening sweep (11c group A) ----

def _make_with(tmp_path: Path, clock: FakeClock, google: FakeGoogleVerifier, base: str = "http://localhost", **over):  # type: ignore[no-untyped-def]
    # Like _make, but with extra AppDeps settings (public_url, secret, redis).
    path = make_working_db(tmp_path / "auth.sqlite3")
    conn = open_connection(path=path)
    admin = users.bootstrap_admin(conn, "boss@x.com", clock)
    users.invite(conn, actor=admin, email="ok@x.com", role="ADMIN", display_name="Ok", clock=clock)
    conn.close()
    kwargs = {"redis": fakeredis.FakeRedis(decode_responses=True), "session_secret": SECRET, **over}
    deps = AppDeps(db_path=path, webhook_secret=b"w", coordinator_tokens={}, fallback_path=tmp_path / "f.jsonl", clock=clock,
                   google=google, **kwargs)
    return path, deps, TestClient(create_app(deps), base_url=base, follow_redirects=False)


def test_public_url_sets_redirect_uri_and_ignores_host(tmp_path: Path, clock: FakeClock) -> None:
    _, _, client = _make_with(tmp_path, clock, FakeGoogleVerifier(), base="http://evil.example", public_url="https://haul.example.com")
    r = client.get("/auth/login", headers={"host": "evil.example"})
    # Cookie is Secure because the public URL is https, whatever the Host header says.
    assert "secure" in r.headers["set-cookie"].lower()


def test_public_url_redirect_uri_used_for_exchange(tmp_path: Path, clock: FakeClock) -> None:
    seen: list[str] = []

    class Spy(FakeGoogleVerifier):
        def authorize_url(self, state: str, code_challenge: str, redirect_uri: str, nonce: str) -> str:
            seen.append(redirect_uri)
            return super().authorize_url(state, code_challenge, redirect_uri, nonce)

        def exchange_and_verify(self, code: str, code_verifier: str, redirect_uri: str, nonce: str) -> GoogleIdentity:
            seen.append(redirect_uri)
            return super().exchange_and_verify(code, code_verifier, redirect_uri, nonce)

    _, _, client = _make_with(tmp_path, clock, Spy(GoogleIdentity("ok@x.com", True, "Ok")), base="https://evil.example",
                              public_url="https://haul.example.com")
    state, _ = _start(client)
    client.get("/auth/callback", params={"code": "c", "state": state})
    assert seen == ["https://haul.example.com/auth/callback"] * 2


def test_public_url_http_is_not_secure(tmp_path: Path, clock: FakeClock) -> None:
    _, _, client = _make_with(tmp_path, clock, FakeGoogleVerifier(), base="https://app.example.com", public_url="http://10.0.0.5:8000")
    assert "secure" not in client.get("/auth/login").headers["set-cookie"].lower()


def test_unset_public_url_uses_request_host(tmp_path: Path, clock: FakeClock) -> None:
    seen: list[str] = []

    class Spy(FakeGoogleVerifier):
        def authorize_url(self, state: str, code_challenge: str, redirect_uri: str, nonce: str) -> str:
            seen.append(redirect_uri)
            return super().authorize_url(state, code_challenge, redirect_uri, nonce)

    _, _, client = _make_with(tmp_path, clock, Spy(), base="https://app.example.com")
    client.get("/auth/login")
    assert seen == ["https://app.example.com/auth/callback"]


@pytest.mark.parametrize("bad", ["haul.example.com", "ftp://haul.example.com", "https://haul.example.com/app",
                                 "https://haul.example.com?x=1", "https://", "https://h.example.com#f", "https://h.example.com/"])
def test_bad_public_url_refused_at_startup(tmp_path: Path, clock: FakeClock, bad: str) -> None:
    with pytest.raises(ValueError, match="SETUHAUL_PUBLIC_URL"):
        _make_with(tmp_path, clock, FakeGoogleVerifier(), public_url=bad)


def test_google_error_param_is_audited_as_google_error(tmp_path: Path, clock: FakeClock) -> None:
    path, _, client = _make_with(tmp_path, clock, FakeGoogleVerifier(GoogleIdentity("ok@x.com", True, "Ok")))
    _start(client)
    r = client.get("/auth/callback", params={"error": "access_denied-SECRET-TEXT"})
    assert r.status_code == 400 and r.json()["reason_code"] == "GOOGLE_ERROR"
    rows = _rows(path, "LOGIN_REFUSED")
    assert len(rows) == 1 and "GOOGLE_ERROR" in rows[0]["detail_json"]
    # Only the fixed reason is stored, never Google's text.
    assert "SECRET-TEXT" not in str(tuple(rows[0]))
    # The state cookie is cleared.
    assert 'setuhaul_oauth=""' in r.headers["set-cookie"] or "max-age=0" in r.headers["set-cookie"].lower()


def test_non_ascii_state_is_a_clean_400(tmp_path: Path, clock: FakeClock) -> None:
    path, _, client = _make_with(tmp_path, clock, FakeGoogleVerifier(GoogleIdentity("ok@x.com", True, "Ok")))
    _start(client)
    r = client.get("/auth/callback", params={"code": "c", "state": "sté\u00e4te"})
    assert r.status_code == 400 and r.json()["reason_code"] == "BAD_STATE"
    assert len(_rows(path, "LOGIN_REFUSED")) == 1


def test_non_ascii_cookie_is_a_clean_400(tmp_path: Path, clock: FakeClock) -> None:
    from setuhaul.auth import routes

    _, _, client = _make_with(tmp_path, clock, FakeGoogleVerifier(GoogleIdentity("ok@x.com", True, "Ok")))
    # A validly signed cookie whose stored state is non-ASCII, against an ASCII query state.
    sealed = routes._seal(SECRET, {"state": "sté\u00e4te", "verifier": "v", "nonce": "n", "exp": clock.now().timestamp() + 100})
    client.cookies.set("setuhaul_oauth", sealed)
    r = client.get("/auth/callback", params={"code": "c", "state": "plain"})
    assert r.status_code == 400 and r.json()["reason_code"] == "BAD_STATE"
    # And the same non-ASCII value on both sides must not crash either.
    r2 = client.get("/auth/callback", params={"code": "c", "state": "sté\u00e4te"})
    assert r2.status_code in (302, 401, 400)


def test_short_session_secret_keeps_google_off(tmp_path: Path, clock: FakeClock, caplog: pytest.LogCaptureFixture) -> None:
    secret = b"short-secret-31-bytes-xxxxxxxxx"[:31]
    with caplog.at_level("WARNING"):
        _, _, client = _make_with(tmp_path, clock, FakeGoogleVerifier(), session_secret=secret)
    assert client.get("/auth/login").status_code == 503
    assert client.get("/auth/callback", params={"code": "c", "state": "s"}).status_code == 503
    assert "SETUHAUL_SESSION_SECRET" in caplog.text and secret.decode() not in caplog.text


def test_route_passes_cookie_verifier_and_nonce(tmp_path: Path, clock: FakeClock) -> None:
    from setuhaul.auth import routes

    fake = FakeGoogleVerifier(GoogleIdentity("ok@x.com", True, "Ok"))
    _, _, client = _make_with(tmp_path, clock, fake)
    state, cookie = _start(client)
    client.get("/auth/callback", params={"code": "c", "state": state})
    data = routes._open(SECRET, cookie, clock.now().timestamp())
    assert data is not None
    assert fake.last_verifier == data["verifier"] and fake.last_nonce == data["nonce"]
    assert fake.last_verifier != state and fake.last_nonce != state and fake.last_verifier != fake.last_nonce


def test_redis_outage_is_an_audited_503(tmp_path: Path, clock: FakeClock) -> None:
    import redis as redis_lib

    class Down:
        def set(self, *a: object, **k: object) -> bool:
            raise redis_lib.exceptions.ConnectionError("down")

    path, _, client = _make_with(tmp_path, clock, FakeGoogleVerifier(GoogleIdentity("ok@x.com", True, "Ok")), redis=Down())
    state, _ = _start(client)
    r = client.get("/auth/callback", params={"code": "c", "state": state})
    assert r.status_code == 503 and r.json()["reason_code"] == "STATE_STORE_UNAVAILABLE"
    assert "STATE_STORE_UNAVAILABLE" in _rows(path, "LOGIN_REFUSED")[0]["detail_json"]
