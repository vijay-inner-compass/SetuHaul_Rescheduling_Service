"""Batch 5 security hardening: V5 trusted proxies, F12 abuse limits/body caps/request-id, F18 POST driver status."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from pathlib import Path

import fakeredis
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from setuhaul.api import AppDeps, create_app
from setuhaul.auth import local_admin, users
from setuhaul.auth.client_ip import client_ip, parse_trusted_proxies
from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from setuhaul.identity import provision_identities
from setuhaul.mail_transport import InMemoryMailbox

SECRET = b"test-webhook-secret"
TG_SECRET = b"0123456789abcdef0123456789abcdef"
PASSWORD = "correct horse battery staple"


def _sign(body: bytes, secret: bytes = SECRET) -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def _setup_app(
    db_path: Path,
    clock: FakeClock,
    *,
    redis=None,  # type: ignore[no-untyped-def]
    trusted_proxies: str = "",
    local_admin_enabled: bool = True,
    password_sender: InMemoryMailbox | None = None,
) -> tuple[TestClient, str]:
    conn = open_connection(path=db_path)
    try:
        admin = users.bootstrap_admin(conn, "boss@x.com", clock)
        assert admin is not None
        local_admin.set_password(conn, admin.user_id, PASSWORD, clock)
        provision_identities(conn)
        phone = conn.execute(
            "select d.phone from drivers d join shipments s using(driver_id) where s.shipment_id='SHP1006'"
        ).fetchone()[0]
    finally:
        conn.close()
    deps = AppDeps(
        db_path=db_path,
        redis=redis if redis is not None else fakeredis.FakeRedis(decode_responses=True),
        webhook_secret=SECRET,
        coordinator_tokens={},
        fallback_path=db_path.parent / "fallback.jsonl",
        clock=clock,
        local_admin_enabled=local_admin_enabled,
        public_url="https://setuhaul.test" if password_sender is not None else "",
        password_sender=password_sender,
        telegram_secret=TG_SECRET,
        trusted_proxies=parse_trusted_proxies(trusted_proxies),
    )
    return TestClient(create_app(deps)), str(phone)


# ---- V5: trusted proxy / client IP -------------------------------------------------------------------------

def test_client_ip_ignores_forwarded_header_from_untrusted_peer() -> None:
    class _Client:
        host = "198.51.100.7"

    class _Req:
        def __init__(self) -> None:
            self.client = _Client()
            self.headers = {"x-forwarded-for": "203.0.113.99"}

    assert client_ip(_Req(), parse_trusted_proxies("172.16.0.0/12")) == "198.51.100.7"  # type: ignore[arg-type]
    assert client_ip(_Req(), ()) == "198.51.100.7"  # type: ignore[arg-type]


def test_client_ip_uses_rightmost_untrusted_forwarded_ip_from_trusted_proxy() -> None:
    class _Client:
        host = "172.18.0.2"

    class _Req:
        def __init__(self) -> None:
            self.client = _Client()
            # Attacker spoofed 1.2.3.4; Caddy appended the real client 203.0.113.42.
            self.headers = {"x-forwarded-for": "1.2.3.4, 203.0.113.42"}

    assert client_ip(_Req(), parse_trusted_proxies("127.0.0.1,172.16.0.0/12")) == "203.0.113.42"  # type: ignore[arg-type]


# ---- F12: sign-in throttling -------------------------------------------------------------------------------

def test_auth_routes_rate_limit_bursts_with_429_and_stop_audit_flood(working_db_path: Path, clock: FakeClock) -> None:
    box = InMemoryMailbox()
    client, _ = _setup_app(working_db_path, clock, password_sender=box, trusted_proxies="127.0.0.1,testclient")
    # 1. /auth/local-admin: 10 tries allowed, 11th returns 429 and writes no audit row.
    for _ in range(10):
        r = client.post("/auth/local-admin", json={"email": "nobody@x.com", "password": "wrong password!!"})
        assert r.status_code == 401
    conn = open_connection(path=working_db_path, readonly=True)
    before = conn.execute("select count(*) from audit_log").fetchone()[0]
    conn.close()
    blocked = client.post("/auth/local-admin", json={"email": "nobody@x.com", "password": "wrong password!!"})
    assert blocked.status_code == 429
    assert blocked.json()["reason_code"] == "RATE_LIMITED"
    assert int(blocked.headers["retry-after"]) > 0
    conn = open_connection(path=working_db_path, readonly=True)
    after = conn.execute("select count(*) from audit_log").fetchone()[0]
    conn.close()
    assert after == before

    # 2. /auth/password: 10 tries allowed, 11th returns 429.
    for _ in range(10):
        r = client.post("/auth/password", json={"email": "nobody@x.com", "password": "wrong password!!"})
        assert r.status_code == 401
    assert client.post("/auth/password", json={"email": "nobody@x.com", "password": "wrong password!!"}).status_code == 429

    # 3. /auth/password/set: 10 tries allowed, 11th returns 429.
    for _ in range(10):
        r = client.post("/auth/password/set", json={"token": "bad", "password": "long enough password"})
        assert r.status_code == 422
    assert client.post("/auth/password/set", json={"token": "bad", "password": "long enough password"}).status_code == 429

    # 4. /auth/password/forgot: 10 tries allowed, 11th returns 429.
    for _ in range(10):
        r = client.post("/auth/password/forgot", json={"email": "nobody@x.com"})
        assert r.status_code == 200
    assert client.post("/auth/password/forgot", json={"email": "nobody@x.com"}).status_code == 429


def test_auth_rate_limit_enforced_when_redis_is_down(working_db_path: Path, clock: FakeClock) -> None:
    class DownRedis:
        def pipeline(self):  # type: ignore[no-untyped-def]
            raise RedisConnectionError("redis down")

        def ping(self) -> bool:
            raise RedisConnectionError("redis down")

    client, _ = _setup_app(working_db_path, clock, redis=DownRedis())
    # Break-glass admin can still sign in when Redis is down.
    ok = client.post("/auth/local-admin", json={"email": "boss@x.com", "password": PASSWORD})
    assert ok.status_code == 200
    # And bursts from the same IP are still capped at 10 per 15 min via in-process fallback.
    for _ in range(9):
        client.post("/auth/local-admin", json={"email": "nobody@x.com", "password": "wrong password!!"})
    blocked = client.post("/auth/local-admin", json={"email": "nobody@x.com", "password": "wrong password!!"})
    assert blocked.status_code == 429


def test_stranger_wrong_passwords_lock_only_stranger_ip_not_admin(working_db_path: Path, clock: FakeClock) -> None:
    # Trust the TestClient peer ("testclient") so X-Forwarded-For simulates distinct client IPs behind Caddy.
    client, _ = _setup_app(working_db_path, clock, trusted_proxies="testclient")
    # Stranger at 198.51.100.9 sends 5 wrong passwords for boss@x.com.
    for _ in range(5):
        r = client.post(
            "/auth/local-admin",
            json={"email": "boss@x.com", "password": "wrong password!!"},
            headers={"X-Forwarded-For": "198.51.100.9"},
        )
        assert r.status_code == 401
    # Stranger IP is now locked even if they somehow guessed right.
    r_stranger = client.post(
        "/auth/local-admin",
        json={"email": "boss@x.com", "password": PASSWORD},
        headers={"X-Forwarded-For": "198.51.100.9"},
    )
    assert r_stranger.status_code == 401
    # Legitimate admin from a different IP (203.0.113.5) is NOT locked out!
    r_admin = client.post(
        "/auth/local-admin",
        json={"email": "boss@x.com", "password": PASSWORD},
        headers={"X-Forwarded-For": "203.0.113.5"},
    )
    assert r_admin.status_code == 200


# ---- F12: request body cap & x-request-id sanitisation -----------------------------------------------------

def test_oversized_body_refused_early_with_413(working_db_path: Path, clock: FakeClock) -> None:
    client, _ = _setup_app(working_db_path, clock)
    big_msg = b"x" * (65 * 1024)
    r1 = client.post("/v1/driver/messages", content=big_msg, headers={"x-signature": "invalid"})
    assert r1.status_code == 413 and r1.json()["reason_code"] == "PAYLOAD_TOO_LARGE"

    big_gps = b"x" * (17 * 1024)
    r2 = client.post("/v1/gps/pings", content=big_gps, headers={"x-signature": "invalid"})
    assert r2.status_code == 413 and r2.json()["reason_code"] == "PAYLOAD_TOO_LARGE"

    r3 = client.post("/v1/driver/location", content=big_gps, headers={"x-signature": "invalid"})
    assert r3.status_code == 413 and r3.json()["reason_code"] == "PAYLOAD_TOO_LARGE"

    big_tg = b"x" * (257 * 1024)
    r4 = client.post(
        "/v1/telegram/webhook",
        content=big_tg,
        headers={"x-telegram-bot-api-secret-token": TG_SECRET.decode()},
    )
    assert r4.status_code == 413 and r4.json()["reason_code"] == "PAYLOAD_TOO_LARGE"


def test_x_request_id_is_bounded_and_sanitised(working_db_path: Path, clock: FakeClock) -> None:
    client, _ = _setup_app(working_db_path, clock)
    # Valid id is echoed.
    r_ok = client.get("/health", headers={"X-Request-ID": "req_123.abc-XYZ"})
    assert r_ok.headers["x-request-id"] == "req_123.abc-XYZ"
    # Overlong id (> 64 chars) is replaced with a generated hex uuid.
    r_long = client.get("/health", headers={"X-Request-ID": "a" * 65})
    assert len(r_long.headers["x-request-id"]) == 32
    assert r_long.headers["x-request-id"] != "a" * 65
    # Unsafe characters (spaces, quotes, semicolons) are rejected and replaced.
    r_bad = client.get("/health", headers={"X-Request-ID": "bad id; injected=1"})
    assert len(r_bad.headers["x-request-id"]) == 32
    assert " " not in r_bad.headers["x-request-id"]


# ---- F18: POST /v1/driver/status (and GET transition) ------------------------------------------------------

def test_driver_status_post_form_verifies_signature_and_timestamp(working_db_path: Path, clock: FakeClock) -> None:
    client, phone = _setup_app(working_db_path, clock)
    ts = int(clock.now().timestamp())
    raw = json.dumps({"phone": phone, "ts": ts}).encode()
    r = client.post("/v1/driver/status", content=raw, headers={"x-signature": _sign(raw)})
    assert r.status_code == 200
    assert "case" in r.json() and "exceptions" in r.json()

    # Bad signature rejected.
    r_bad = client.post("/v1/driver/status", content=raw, headers={"x-signature": "0" * 64})
    assert r_bad.status_code == 401

    # Stale timestamp rejected.
    stale_raw = json.dumps({"phone": phone, "ts": int((clock.now() - timedelta(minutes=10)).timestamp())}).encode()
    r_stale = client.post("/v1/driver/status", content=stale_raw, headers={"x-signature": _sign(stale_raw)})
    assert r_stale.status_code == 401 and r_stale.json()["reason_code"] == "STALE_REQUEST"

    # GET transition form still works and marks itself deprecated.
    sig_get = _sign(f"{phone}|{ts}".encode())
    r_get = client.get("/v1/driver/status", params={"phone": phone, "ts": ts}, headers={"x-signature": sig_get})
    assert r_get.status_code == 200
    assert r_get.headers.get("deprecation") == "true"
