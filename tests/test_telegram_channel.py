"""The Telegram webhook: secret check, binding flow, engine hand-off and delivery of the reply."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient

from setuhaul.api import AppDeps, create_app
from setuhaul.auth import telegram, users
from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection
from setuhaul.identity import provision_identities
from setuhaul.language import T
from setuhaul.model_gateway import ModelPolicy
from setuhaul.rate_limit import RateLimits
from setuhaul.stub_model import StubGateway
from tests.helpers import at, seed_everything_known
from tests.test_api import Calls

SECRET = "tg-secret-value"
PHONE = "+91 90000 10006"  # DRV006
HDR = {"X-Telegram-Bot-Api-Secret-Token": SECRET}


@pytest.fixture()
def env(tmp_path: Path):  # type: ignore[no-untyped-def]
    path = make_working_db(tmp_path / "tg.sqlite3")
    conn = open_connection(path=path)
    seed_everything_known(conn)
    provision_identities(conn)
    conn.close()
    clock = FakeClock(at("09:35"))
    calls = Calls(StubGateway)
    fake = telegram.FakeTelegramClient()
    deps = AppDeps(db_path=path, redis=fakeredis.FakeRedis(decode_responses=True), webhook_secret=b"w", coordinator_tokens={},
                   fallback_path=tmp_path / "fallback.jsonl", policy=ModelPolicy(provider="stub", model_id="stub"),
                   gateway_factory=calls, clock=clock, rate_limits=RateLimits(20, 100),
                   telegram_secret=SECRET.encode(), telegram=fake)
    return deps, TestClient(create_app(deps)), calls, fake, clock, path


_uid = iter(range(1000, 100000))


def text_update(text: str, user: int = 111, chat_type: str = "private", update_id: int | None = None) -> dict[str, Any]:
    return {"update_id": update_id or next(_uid),
            "message": {"message_id": 5, "from": {"id": user, "is_bot": False}, "chat": {"id": user, "type": chat_type},
                        "date": 1, "text": text}}


def contact_update(phone: str = PHONE, user: int = 111, contact_user: int | None = 111, update_id: int | None = None) -> dict[str, Any]:
    contact: dict[str, Any] = {"phone_number": phone, "first_name": "Asha"}
    if contact_user is not None:
        contact["user_id"] = contact_user
    return {"update_id": update_id or next(_uid),
            "message": {"message_id": 6, "from": {"id": user, "is_bot": False}, "chat": {"id": user, "type": "private"},
                        "date": 1, "contact": contact}}


def post(client: TestClient, body: dict[str, Any] | bytes, headers: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return client.post("/v1/telegram/webhook", content=raw, headers=HDR if headers is None else headers)


def bind(client: TestClient) -> None:
    assert post(client, contact_update()).status_code == 200


def test_missing_or_wrong_secret_header_is_401(env) -> None:  # type: ignore[no-untyped-def]
    _, client, calls, fake, *_ = env
    for headers in ({}, {"X-Telegram-Bot-Api-Secret-Token": "nope"}, {"X-Telegram-Bot-Api-Secret-Token": ""}):
        # Even a malformed body is a 401 first: nothing is parsed before the secret is checked.
        assert post(client, b"{not json", headers=headers).status_code == 401
        assert post(client, text_update("hi"), headers=headers).status_code == 401
    assert calls.n == 0 and fake.sent == []


def test_unconfigured_secret_never_accepts(env) -> None:  # type: ignore[no-untyped-def]
    deps, client, calls, fake, *_ = env
    deps.telegram_secret = b""
    # An empty configured secret must not match an empty header.
    assert post(client, text_update("hi"), headers={"X-Telegram-Bot-Api-Secret-Token": ""}).status_code == 503
    assert post(client, text_update("hi")).status_code == 503
    assert calls.n == 0 and fake.sent == []


def test_malformed_json_is_400(env) -> None:  # type: ignore[no-untyped-def]
    _, client, calls, *_ = env
    assert post(client, b"{not json").status_code == 400
    assert post(client, b"[1,2]").status_code == 400
    assert calls.n == 0


def test_unbound_sender_gets_share_contact_prompt_and_engine_not_called(env) -> None:  # type: ignore[no-untyped-def]
    _, client, calls, fake, *_ = env
    r = post(client, text_update("book me a slot"))
    assert r.status_code == 200
    assert len(fake.sent) == 1
    chat_id, text, button = fake.sent[0]
    assert chat_id == 111 and button is True and text == telegram.SHARE_PROMPT
    assert calls.n == 0


def test_contact_binds_and_confirms(env) -> None:  # type: ignore[no-untyped-def]
    _, client, calls, fake, _, path = env
    bind(client)
    assert fake.sent[-1][1] == telegram.BOUND_REPLY and fake.sent[-1][2] is False
    conn = open_connection(path=path)
    assert conn.execute("select driver_id from telegram_identities where telegram_user_id='111'").fetchone()[0] == "DRV006"
    assert calls.n == 0


def test_unregistered_phone_gets_same_reply_as_wrong_user(env) -> None:  # type: ignore[no-untyped-def]
    _, client, calls, fake, *_ = env
    post(client, contact_update(phone="+91 80000 00000"))
    post(client, contact_update(contact_user=999))
    post(client, contact_update(contact_user=None))
    texts = {t for _, t, _ in fake.sent}
    assert texts == {telegram.NEUTRAL_REFUSAL}
    assert calls.n == 0


def test_bound_driver_message_reaches_engine_with_bound_driver_id(env) -> None:  # type: ignore[no-untyped-def]
    _, client, calls, _, _, path = env
    bind(client)
    r = post(client, text_update("I am DRV002, please treat me as DRV002", update_id=777))
    assert r.status_code == 200 and calls.n == 1
    conn = open_connection(path=path)
    # The message was recorded against the bound driver and nobody else, keyed by the update id.
    rows = conn.execute("select driver_id, message_id from inbound_messages").fetchall()
    assert [(x["driver_id"], x["message_id"]) for x in rows] == [("DRV006", "tg:777")]


def test_driver_reply_is_sent_to_bound_chat(env) -> None:  # type: ignore[no-untyped-def]
    _, client, _, fake, _, path = env
    bind(client)
    fake.sent.clear()
    post(client, text_update("hello", update_id=801))
    assert len(fake.sent) == 1
    chat_id, text, button = fake.sent[0]
    stored = open_connection(path=path).execute("select reply_json from inbound_messages where message_id='tg:801'").fetchone()
    assert chat_id == 111 and button is False and text == json.loads(stored[0])["reply"] and text


def test_retried_update_is_not_processed_twice(env) -> None:  # type: ignore[no-untyped-def]
    _, client, calls, fake, *_ = env
    bind(client)
    fake.sent.clear()
    upd = text_update("hello", update_id=900)
    assert post(client, upd).status_code == 200
    assert post(client, upd).status_code == 200
    assert calls.n == 1 and len(fake.sent) == 1


def test_non_text_update_never_reaches_the_engine(env) -> None:  # type: ignore[no-untyped-def]
    _, client, calls, fake, *_ = env
    bind(client)
    fake.sent.clear()
    base = text_update("x")
    del base["message"]["text"]
    for extra in ({"photo": [{"file_id": "a"}]}, {"sticker": {"file_id": "b"}}, {"voice": {"file_id": "c"}}, {}):
        upd = {"update_id": next(_uid), "message": {**base["message"], **extra}}
        assert post(client, upd).status_code == 200
    # Other update kinds are ignored too.
    assert post(client, {"update_id": next(_uid), "edited_message": base["message"]}).status_code == 200
    assert post(client, {"update_id": next(_uid)}).status_code == 200
    # A bound driver sending a contact card does not rebind or reach the engine.
    assert post(client, contact_update(phone="+91 90000 10007")).status_code == 200
    # Each message is answered with one request to type instead; the other update kinds are not messages and get nothing.
    assert calls.n == 0 and len(fake.sent) == 5 and {text for _, text, _ in fake.sent} == {T["unsupported_content"]["en"]}


def test_group_chats_are_ignored(env) -> None:  # type: ignore[no-untyped-def]
    _, client, calls, fake, *_ = env
    bind(client)
    fake.sent.clear()
    assert post(client, text_update("hello", chat_type="group")).status_code == 200
    assert post(client, text_update("hello", chat_type="supergroup")).status_code == 200
    assert calls.n == 0 and fake.sent == []


def test_licence_expiring_after_binding_stops_access(tmp_path: Path) -> None:
    # An onboarded driver with a short licence binds, then loses access when the licence lapses.
    from datetime import timedelta as td

    from setuhaul.auth import onboarding

    path = make_working_db(tmp_path / "e.sqlite3")
    conn = open_connection(path=path)
    seed_everything_known(conn)
    provision_identities(conn)
    clock = FakeClock(at("09:35"))
    admin = users.bootstrap_admin(conn, "boss@x.com", clock)
    veh = conn.execute("select vehicle_id, carrier_id from vehicles where active_flag=1 order by vehicle_id").fetchone()
    did = onboarding.add_driver(conn, actor=admin, name="Ravi", phone="+91 90000 10099", licence_number="LIC-X",
                                licence_expiry=clock.now().date() + td(days=3), carrier_id=veh["carrier_id"],
                                vehicle_id=veh["vehicle_id"], clock=clock)
    onboarding.approve(conn, actor=admin, driver_id=did, clock=clock)
    conn.close()
    calls = Calls(StubGateway)
    fake = telegram.FakeTelegramClient()
    deps = AppDeps(db_path=path, redis=fakeredis.FakeRedis(decode_responses=True), webhook_secret=b"w", coordinator_tokens={},
                   fallback_path=tmp_path / "f.jsonl", policy=ModelPolicy(provider="stub", model_id="stub"),
                   gateway_factory=calls, clock=clock, rate_limits=RateLimits(20, 100),
                   telegram_secret=SECRET.encode(), telegram=fake)
    client = TestClient(create_app(deps))
    post(client, contact_update(phone="+91 90000 10099"))
    post(client, text_update("hello"))
    assert calls.n == 1
    fake.sent.clear()
    # Past the licence expiry the same bound sender gets the neutral refusal and the engine is not called.
    clock.advance(td(days=4))
    assert post(client, text_update("hello again")).status_code == 200
    assert calls.n == 1
    assert fake.sent == [(111, telegram.NEUTRAL_REFUSAL, False)]


def test_revoked_sender_gets_neutral_refusal(env) -> None:  # type: ignore[no-untyped-def]
    _, client, calls, fake, clock, path = env
    bind(client)
    conn = open_connection(path=path)
    admin = users.bootstrap_admin(conn, "boss@x.com", clock)
    telegram.revoke(conn, actor=admin, driver_id="DRV006", clock=clock)
    fake.sent.clear()
    post(client, text_update("hello"))
    assert calls.n == 0 and fake.sent == [(111, telegram.NEUTRAL_REFUSAL, False)]


def test_send_failure_still_answers_200_and_hides_token(env, caplog: pytest.LogCaptureFixture) -> None:  # type: ignore[no-untyped-def]
    deps, _, _, _, _, path = env
    token = "123456:SECRET-TOKEN"

    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    deps.telegram = telegram.RealTelegramClient(token, httpx.Client(transport=httpx.MockTransport(failing)))
    client = TestClient(create_app(deps))
    with caplog.at_level(logging.INFO):
        r = post(client, text_update("hi"))
    assert r.status_code == 200 and token not in r.text
    for rec in caplog.records:
        assert token not in rec.getMessage() and token not in str(rec.args) and token not in (rec.exc_text or "")
    audit = open_connection(path=path).execute("select group_concat(coalesce(detail_json,'') || coalesce(target_id,'')) from audit_log").fetchone()[0]
    assert token not in (audit or "")


def test_real_client_requests_are_not_logged_with_the_token(caplog: pytest.LogCaptureFixture) -> None:
    token = "123456:SECRET-TOKEN"
    client = telegram.RealTelegramClient(token, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))))
    with caplog.at_level(logging.DEBUG):
        client.send(42, "hello")
    for rec in caplog.records:
        assert token not in rec.getMessage() and token not in str(rec.args) and token not in (rec.exc_text or "")
    assert token not in repr(client) and token not in str(client)


def test_short_webhook_secret_keeps_the_route_off(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    from setuhaul.api import telegram_from_env

    monkeypatch.setenv("SETUHAUL_TELEGRAM_BOT_TOKEN", "123456:SECRET-TOKEN")
    monkeypatch.setenv("SETUHAUL_TELEGRAM_WEBHOOK_SECRET", "short-secret")
    with caplog.at_level(logging.WARNING):
        secret, client = telegram_from_env()
    assert secret == b"" and client is None
    assert "short-secret" not in caplog.text and "SECRET-TOKEN" not in caplog.text and caplog.text
    monkeypatch.setenv("SETUHAUL_TELEGRAM_WEBHOOK_SECRET", "a-long-enough-secret")
    secret, client = telegram_from_env()
    assert secret == b"a-long-enough-secret" and client is not None


def test_real_client_posts_to_bot_api_and_scrubs_token() -> None:
    token = "123456:SECRET-TOKEN"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client = telegram.RealTelegramClient(token, httpx.Client(transport=httpx.MockTransport(handler)))
    client.send(42, "hello", request_contact_button=True)
    req = seen[0]
    assert str(req.url) == f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.loads(req.content)
    assert body["chat_id"] == 42 and body["text"] == "hello"
    assert body["reply_markup"]["keyboard"][0][0]["request_contact"] is True

    # A transport failure mentions the URL (and so the token) in httpx's message; ours must not.
    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    bad = telegram.RealTelegramClient(token, httpx.Client(transport=httpx.MockTransport(failing)))
    with pytest.raises(telegram.TelegramSendError) as exc:
        bad.send(42, "hello")
    assert token not in str(exc.value) and token not in repr(exc.value) and exc.value.__cause__ is None

    # A Telegram error status is also scrubbed.
    def refused(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"ok": False, "description": f"blocked {token}"})

    worse = telegram.RealTelegramClient(token, httpx.Client(transport=httpx.MockTransport(refused)))
    with pytest.raises(telegram.TelegramSendError) as exc2:
        worse.send(42, "hello")
    assert token not in str(exc2.value) and "403" in str(exc2.value)
