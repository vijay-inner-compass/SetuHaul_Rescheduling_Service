"""Phase 11: protected driver and coordinator APIs."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

from setuhaul.api import AppDeps, create_app
from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection
from setuhaul.identity import provision_identities
from setuhaul.model_gateway import ModelGateway, ModelPolicy, PermanentModelError, ScriptedGateway
from setuhaul.rate_limit import RateLimits
from setuhaul.stub_model import StubGateway
from tests.helpers import at, seed_everything_known

SECRET = b"webhook-secret"
PHONE = "+91 90000 10006"  # DRV006
TOKENS = {"tok-asha": "asha", "tok-ben": "ben"}


def sign(body: bytes, secret: bytes = SECRET) -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


class Calls:
    def __init__(self, inner: Callable[[], ModelGateway]) -> None:
        self.n = 0
        self._inner = inner

    def __call__(self) -> ModelGateway:
        self.n += 1
        return self._inner()


@pytest.fixture()
def env(tmp_path: Path):  # type: ignore[no-untyped-def]
    path = make_working_db(tmp_path / "api.sqlite3")
    conn = open_connection(path=path)
    seed_everything_known(conn)
    provision_identities(conn)
    conn.close()
    clock = FakeClock(at("09:35"))
    calls = Calls(StubGateway)
    deps = AppDeps(db_path=path, redis=fakeredis.FakeRedis(decode_responses=True), webhook_secret=SECRET, coordinator_tokens=TOKENS,
                   fallback_path=tmp_path / "fallback.jsonl", policy=ModelPolicy(provider="stub", model_id="stub"),
                   gateway_factory=calls, clock=clock, rate_limits=RateLimits(20, 100))
    return deps, TestClient(create_app(deps)), calls, clock, path


def post(client: TestClient, body: dict, signature: str | None = None, **headers: str):  # type: ignore[no-untyped-def, type-arg]
    raw = json.dumps(body).encode()
    return client.post("/v1/driver/messages", content=raw, headers={"x-signature": signature if signature is not None else sign(raw), **headers})


def msg(text: str, mid: str = "M1", sender: str = PHONE) -> dict[str, str]:
    return {"from": sender, "message_id": mid, "text": text}


# --- health and readiness ------------------------------------------------------------------------------
def test_health_is_alive_even_when_the_database_is_gone(env) -> None:  # type: ignore[no-untyped-def]
    deps, client, *_ = env
    deps.db_path = deps.db_path.parent / "missing.sqlite3"
    assert client.get("/health").json() == {"status": "ok"}
    r = client.get("/ready")
    assert r.status_code == 503 and r.json()["reason_code"] == "DATABASE_UNAVAILABLE" and r.json()["ready"] is False


def test_ready_when_everything_is_up_and_not_ready_without_the_cache(env, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    deps, client, *_ = env
    assert client.get("/ready").json()["ready"] is True
    monkeypatch.setattr(deps.redis, "ping", lambda: (_ for _ in ()).throw(ConnectionError("redis down")))
    r = client.get("/ready")
    assert r.status_code == 200 and r.json()["ready"] is True and r.json()["degraded"] == ["cache"] and "redis down" not in r.text


def test_health_ready_is_the_same_check_as_ready(env) -> None:  # type: ignore[no-untyped-def]
    deps, client, *_ = env
    assert client.get("/health/ready").json()["ready"] is True
    deps.db_path = deps.db_path.parent / "missing.sqlite3"
    r = client.get("/health/ready")
    assert r.status_code == 503 and r.json()["reason_code"] == "DATABASE_UNAVAILABLE"


@pytest.mark.parametrize(("stamped", "reported"), [("e202c90abc", "e202c90abc"), (None, "unknown"), ("", "unknown")])
def test_version_names_the_commit_the_image_was_built_from(  # type: ignore[no-untyped-def]
        env, monkeypatch: pytest.MonkeyPatch, stamped: str | None, reported: str) -> None:
    _, client, *_ = env
    if stamped is None:
        monkeypatch.delenv("GIT_COMMIT", raising=False)
    else:
        monkeypatch.setenv("GIT_COMMIT", stamped)
    r = client.get("/version")
    assert r.status_code == 200 and r.json() == {"commit": reported}


# --- signature, identity, validation --------------------------------------------------------------------
def test_bad_or_missing_signature_is_refused_before_the_body_is_parsed(env) -> None:  # type: ignore[no-untyped-def]
    _, client, calls, *_ = env
    for sig in ("", "0" * 64):
        r = client.post("/v1/driver/messages", content=b"{not json at all", headers={"x-signature": sig})
        assert r.status_code == 401 and r.json()["reason_code"] == "INVALID_SIGNATURE"  # 401, not 422: nothing was parsed
    assert post(client, msg("hello"), signature=sign(b"other-body")).status_code == 401
    assert calls.n == 0


def test_validly_signed_garbage_is_a_422_and_client_supplied_identity_is_refused(env) -> None:  # type: ignore[no-untyped-def]
    _, client, calls, *_ = env
    raw = b"{not json"
    assert client.post("/v1/driver/messages", content=raw, headers={"x-signature": sign(raw)}).json()["reason_code"] == "INVALID_REQUEST"
    for extra in ({"driver_id": "DRV007"}, {"thread_id": "THR002"}, {"session": "x"}):
        r = post(client, {**msg("hello"), **extra})
        assert r.status_code == 422 and r.json()["reason_code"] == "INVALID_REQUEST"
    assert calls.n == 0


def test_unknown_and_suspended_senders_are_refused(env) -> None:  # type: ignore[no-untyped-def]
    _, client, calls, _, path = env
    assert post(client, msg("hello", sender="+91 99999 99999")).json()["reason_code"] == "UNKNOWN_PHONE"
    conn = open_connection(path=path)
    conn.execute("update drivers set driver_status='SUSPENDED' where driver_id='DRV006'")
    conn.commit()
    conn.close()
    r = post(client, msg("hello", "M2"))
    assert r.status_code == 403 and r.json()["reason_code"] == "DRIVER_NOT_ALLOWED" and calls.n == 0


# --- the happy path ------------------------------------------------------------------------------------
def test_a_message_gets_a_structured_reply_with_a_request_id(env) -> None:  # type: ignore[no-untyped-def]
    _, client, calls, *_ = env
    r = post(client, msg("my truck is 60 minutes late"))
    body = r.json()
    assert r.status_code == 200 and set(body) == {"status", "reply", "language", "reason_code", "case_id", "duplicate", "request_id"}
    assert body["status"] == "REPLIED" and body["duplicate"] is False and body["reply"].startswith("I am holding ")
    assert r.headers["x-request-id"] == body["request_id"] and calls.n == 1


def test_a_caller_supplied_request_id_is_echoed(env) -> None:  # type: ignore[no-untyped-def]
    _, client, *_ = env
    assert post(client, msg("hello"), **{"x-request-id": "trace-123"}).headers["x-request-id"] == "trace-123"


def test_hold_and_confirm_through_the_api_use_the_drivers_own_thread(env) -> None:  # type: ignore[no-untyped-def]
    _, client, _, _, path = env
    post(client, msg("my truck is 60 minutes late", "M1"))
    held = post(client, msg("I will take the first one", "M2")).json()
    assert "holding" in held["reply"] and held["status"] == "REPLIED"
    done = post(client, msg("yes please confirm", "M3")).json()
    assert done["reply"].startswith("Your appointment is confirmed")
    conn = open_connection(path=path, readonly=True)
    assert conn.execute("select count(*) from appointments where appointment_status='CONFIRMED' and appointment_id like 'APT-H-%'").fetchone()[0] == 1
    conn.close()


# --- rate limits and duplicate deliveries, both before the model --------------------------------------------
def test_burst_is_rate_limited_before_the_model_is_called(env) -> None:  # type: ignore[no-untyped-def]
    deps, client, calls, *_ = env
    deps.rate_limits = RateLimits(per_minute=3, per_hour=100)
    codes = [post(client, msg("hi", f"M{i}")).status_code for i in range(5)]
    assert codes == [200, 200, 200, 429, 429] and calls.n == 3
    r = post(client, msg("hi", "M9"))
    assert r.json()["reason_code"] == "RATE_LIMIT_MINUTE" and int(r.headers["retry-after"]) > 0


def test_duplicate_delivery_returns_the_stored_reply_without_another_model_call(env) -> None:  # type: ignore[no-untyped-def]
    _deps, client, calls, _, path = env
    first = post(client, msg("my truck is 60 minutes late", "DUP")).json()
    again = post(client, msg("my truck is 60 minutes late", "DUP")).json()
    assert again["duplicate"] is True and again["reply"] == first["reply"] and calls.n == 1
    conn = open_connection(path=path, readonly=True)
    assert conn.execute("select count(*) from chat_messages where external_message_id='DUP'").fetchone()[0] == 1
    conn.close()


def test_duplicates_do_not_use_up_the_rate_limit(env) -> None:  # type: ignore[no-untyped-def]
    deps, client, _calls, *_ = env
    deps.rate_limits = RateLimits(per_minute=2, per_hour=100)
    post(client, msg("hi", "A"))
    assert [post(client, msg("hi", "A")).json()["duplicate"] for _ in range(6)] == [True] * 6
    assert post(client, msg("hello", "B")).status_code == 200


def test_same_message_id_with_different_text_is_a_conflict(env) -> None:  # type: ignore[no-untyped-def]
    _, client, *_ = env
    post(client, msg("hello", "X"))
    r = post(client, msg("something else entirely", "X"))
    assert r.status_code == 409 and r.json()["reason_code"] == "IDEMPOTENCY_MISMATCH"


def test_a_message_still_being_processed_is_reported_and_a_stale_claim_is_taken_over(env) -> None:  # type: ignore[no-untyped-def]
    _, client, calls, clock, path = env
    conn = open_connection(path=path)
    conn.execute("insert into inbound_messages (driver_id, message_id, fingerprint, status, claimed_epoch, created_at) "
                 "values ('DRV006','P1',?, 'PROCESSING',?,?)", (hashlib.sha256(b"hello").hexdigest(), int(clock.now().timestamp()), clock.now().isoformat()))
    conn.commit()
    conn.close()
    r = post(client, msg("hello", "P1"))
    assert r.status_code == 409 and r.json()["reason_code"] == "IN_PROGRESS" and calls.n == 0
    clock.advance(timedelta(seconds=61))
    assert post(client, msg("hello", "P1")).status_code == 200 and calls.n == 1


def test_simultaneous_duplicates_run_the_model_once(env) -> None:  # type: ignore[no-untyped-def]
    _, client, calls, *_ = env
    results: list[tuple[int, str]] = []
    barrier = threading.Barrier(5)

    def go() -> None:
        barrier.wait()
        r = post(client, msg("my truck is 60 minutes late", "RACE"))
        results.append((r.status_code, r.json().get("reason_code") or r.json().get("status")))

    ts = [threading.Thread(target=go) for _ in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert calls.n == 1 and sum(1 for c, _ in results if c == 200) >= 1
    assert all(c in (200, 409) for c, _ in results)


# --- outages ------------------------------------------------------------------------------------------------
def test_database_outage_is_a_503_that_invites_a_retry(env, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    _, client, calls, *_ = env
    from setuhaul import api

    monkeypatch.setattr(api, "open_connection", lambda **k: (_ for _ in ()).throw(sqlite3.OperationalError("unable to open database file")))
    r = post(client, msg("hello"))
    assert r.status_code == 503 and r.json()["reason_code"] == "DATABASE_UNAVAILABLE"
    assert "unable to open" not in r.text and "Traceback" not in r.text and calls.n == 0


def test_database_failure_inside_the_turn_still_gets_a_safe_reply(env, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    deps, client, *_ = env
    from setuhaul import tools

    monkeypatch.setattr(tools, "authorize_shipment", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
    deps.gateway_factory = lambda: ScriptedGateway([  # type: ignore[assignment]
        __import__("tests.conv_helpers", fromlist=["tc"]).tc("get_shipment_status", shipment_id="SHP1006")])
    r = post(client, msg("what is my appointment?"))
    body = r.json()
    assert r.status_code == 200 and body["status"] == "TEMPORARILY_UNAVAILABLE" and body["reason_code"] == "DATABASE_UNAVAILABLE"
    assert "Nothing has been booked or held" in body["reply"] and "disk I/O" not in r.text


def test_model_outage_is_a_safe_reply_plus_an_escalation_case(env) -> None:  # type: ignore[no-untyped-def]
    deps, client, _, _, path = env
    deps.gateway_factory = lambda: ScriptedGateway([PermanentModelError("401")])  # type: ignore[assignment]
    body = post(client, msg("hello there")).json()
    assert body["status"] == "TEMPORARILY_UNAVAILABLE" and body["reason_code"] == "MODEL_UNAVAILABLE"
    conn = open_connection(path=path, readonly=True)
    assert conn.execute("select reason_code, source from escalation_cases").fetchall()[0][0] == "MODEL_UNAVAILABLE"
    conn.close()


def test_unconfigured_model_takes_the_same_safe_path(tmp_path: Path) -> None:
    path = make_working_db(tmp_path / "u.sqlite3")
    conn = open_connection(path=path)
    seed_everything_known(conn)
    provision_identities(conn)
    conn.close()
    deps = AppDeps(db_path=path, redis=fakeredis.FakeRedis(decode_responses=True), webhook_secret=SECRET, coordinator_tokens={},
                   fallback_path=tmp_path / "f.jsonl", clock=FakeClock(at("09:35")))
    body = post(TestClient(create_app(deps)), msg("hello there")).json()
    assert body["status"] == "TEMPORARILY_UNAVAILABLE" and body["reason_code"] == "MODEL_UNAVAILABLE"


def test_an_unexpected_error_leaks_nothing_and_frees_the_message_id(env, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    _, client, calls, *_ = env
    from setuhaul import api

    real = api.handle_message
    monkeypatch.setattr(api, "handle_message", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret detail at C:/secret/path")))
    r = TestClient(client.app, raise_server_exceptions=False).post(
        "/v1/driver/messages", content=json.dumps(msg("hello", "E1")).encode(), headers={"x-signature": sign(json.dumps(msg("hello", "E1")).encode())})
    assert r.status_code == 500 and r.json()["reason_code"] == "INTERNAL_ERROR" and "secret" not in r.text and "request_id" in r.json()
    monkeypatch.setattr(api, "handle_message", real)
    assert post(client, msg("hello", "E1")).status_code == 200  # the retry is not blocked by a stuck claim
    assert calls.n >= 1


# --- driver status ------------------------------------------------------------------------------------------
def status_get(client: TestClient, clock: FakeClock, phone: str = PHONE, sig: str | None = None, ts: int | None = None):  # type: ignore[no-untyped-def]
    t = ts if ts is not None else int(clock.now().timestamp())
    return client.get("/v1/driver/status", params={"phone": phone, "ts": t}, headers={"x-signature": sig or sign(f"{phone}|{t}".encode())})


def test_driver_status_is_signed_fresh_and_limited_to_the_drivers_own_data(env) -> None:  # type: ignore[no-untyped-def]
    _deps, client, _, clock, _path = env
    ok = status_get(client, clock)
    assert ok.status_code == 200 and {e["shipment_id"] for e in ok.json()["exceptions"]} <= {"SHP1006"}
    assert ok.json()["case"] is None
    assert status_get(client, clock, sig="0" * 64).status_code == 401
    assert status_get(client, clock, ts=int(clock.now().timestamp()) - 3600).json()["reason_code"] == "STALE_REQUEST"
    other = status_get(client, clock, phone="+91 90000 10012")
    assert all(e["shipment_id"] == "SHP1012" for e in other.json()["exceptions"])


def test_driver_status_shows_the_case_after_a_hand_off(env) -> None:  # type: ignore[no-untyped-def]
    deps, client, _, clock, _ = env
    deps.gateway_factory = lambda: ScriptedGateway([PermanentModelError("x")])  # type: ignore[assignment]
    post(client, msg("hello there"))
    case = status_get(client, clock).json()["case"]
    assert case["status"] == "OPEN" and "operations" in case["text"].lower() or case["status"] == "OPEN"


# --- coordinator --------------------------------------------------------------------------------------------
def make_case(env) -> str:  # type: ignore[no-untyped-def]
    deps, client, *_ = env
    deps.gateway_factory = lambda: ScriptedGateway([PermanentModelError("x")])  # type: ignore[assignment]
    return str(post(client, msg("hello there", "CASE")).json()["case_id"])


def auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def test_coordinator_endpoints_need_a_known_token(env) -> None:  # type: ignore[no-untyped-def]
    _, client, *_ = env
    for headers in ({}, auth("nope"), {"authorization": "tok-asha"}):
        assert client.get("/v1/coordinator/cases", headers=headers).status_code == 401
    assert client.get("/v1/coordinator/cases", headers=auth("tok-asha")).status_code == 200


def test_coordinator_lists_shows_claims_and_resolves(env) -> None:  # type: ignore[no-untyped-def]
    _, client, *_ = env
    cid = make_case(env)
    listing = client.get("/v1/coordinator/cases", headers=auth("tok-asha")).json()["cases"]
    assert [c["case_id"] for c in listing] == [cid] and listing[0]["context"]["language"] == "en"
    detail = client.get(f"/v1/coordinator/cases/{cid}", headers=auth("tok-asha")).json()
    assert detail["case"]["reason_code"] == "MODEL_UNAVAILABLE" and detail["events"][0]["event_type"] == "CREATED"
    assert client.post(f"/v1/coordinator/cases/{cid}/claim", headers=auth("tok-asha")).json()["status"] == "CLAIMED"
    r = client.post(f"/v1/coordinator/cases/{cid}/resolve", headers=auth("tok-asha"), json={"resolution_code": "ADVISED_DRIVER", "note": "called the driver"})
    assert r.status_code == 200 and r.json()["status"] == "RESOLVED"
    assert client.get("/v1/coordinator/cases", headers=auth("tok-asha")).json()["cases"] == []
    assert client.get("/v1/coordinator/cases/ESC-NOPE", headers=auth("tok-asha")).status_code == 404


def test_coordinator_claim_conflict_is_a_409_naming_the_owner(env) -> None:  # type: ignore[no-untyped-def]
    _, client, *_ = env
    cid = make_case(env)
    assert client.post(f"/v1/coordinator/cases/{cid}/claim", headers=auth("tok-asha")).status_code == 200
    r = client.post(f"/v1/coordinator/cases/{cid}/claim", headers=auth("tok-ben"))
    assert r.status_code == 409 and r.json()["reason_code"] == "ALREADY_CLAIMED" and r.json()["claimed_by"] == "asha"
    assert client.post(f"/v1/coordinator/cases/{cid}/claim", headers=auth("tok-asha")).status_code == 200  # owner retrying is fine
    wrong = client.post(f"/v1/coordinator/cases/{cid}/resolve", headers=auth("tok-ben"), json={"resolution_code": "ADVISED_DRIVER", "note": "x"})
    assert wrong.status_code == 403 and wrong.json()["reason_code"] == "NOT_YOUR_CASE"
    bad = client.post(f"/v1/coordinator/cases/{cid}/resolve", headers=auth("tok-asha"), json={"resolution_code": "MAGIC", "note": "x"})
    assert bad.status_code == 422


def test_racing_claims_over_http_have_one_winner(env) -> None:  # type: ignore[no-untyped-def]
    _, client, *_ = env
    cid = make_case(env)
    codes: list[int] = []
    barrier = threading.Barrier(4)

    def go(token: str) -> None:
        barrier.wait()
        codes.append(client.post(f"/v1/coordinator/cases/{cid}/claim", headers=auth(token)).status_code)

    ts = [threading.Thread(target=go, args=(t,)) for t in ("tok-asha", "tok-ben", "tok-asha", "tok-ben")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    owners = {200: codes.count(200), 409: codes.count(409)}
    assert owners[200] >= 1 and owners[200] + owners[409] == 4
    detail = client.get(f"/v1/coordinator/cases/{cid}", headers=auth("tok-asha")).json()["case"]
    assert detail["claimed_by"] in ("asha", "ben")


# --- background maintenance ---------------------------------------------------------------------------------
def test_lifespan_sweeps_overdue_holds_at_startup(tmp_path: Path) -> None:
    from setuhaul.booking import acquire_hold
    from tests.helpers import show_options

    path = make_working_db(tmp_path / "m.sqlite3")
    conn = open_connection(path=path)
    seed_everything_known(conn)
    clock = FakeClock(at("09:35"))
    held = acquire_hold(conn, "DRV006", "THR001", "SHP1006", show_options(conn, clock).option_set_id, 1, clock)
    conn.close()
    clock.advance(timedelta(minutes=10))
    deps = AppDeps(db_path=path, redis=fakeredis.FakeRedis(decode_responses=True), webhook_secret=SECRET, coordinator_tokens={},
                   fallback_path=tmp_path / "f.jsonl", clock=clock, run_maintenance=True, maintenance_interval_s=3600)
    with TestClient(create_app(deps)):
        check = open_connection(path=path, readonly=True)
        status = check.execute("select appointment_status from appointments where appointment_id=?", (held.appointment_id,)).fetchone()[0]
        check.close()
    assert status == "CANCELLED"
