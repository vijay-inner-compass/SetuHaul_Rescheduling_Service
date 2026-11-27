"""Test connection, test bot and webhook registration."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from setuhaul.db import open_connection
from setuhaul.runtime_settings import SettingsSource
from tests.auth_helpers import World, build_world
from tests.helpers import at
from tests.runtime_helpers import BASE

URL = "/v1/admin/settings"
TOKEN, SECRET = "123:token-ZZZZ", "webhook-secret-0123456789"


@pytest.fixture()
def world(tmp_path: Path) -> World:
    from setuhaul.clock import FakeClock

    built = build_world(tmp_path, FakeClock(at("09:35")))
    built.deps.public_url = "https://haul.example.com"
    return built


def _use(world: World, handler: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    world.deps.settings = SettingsSource(BASE, transport=httpx.MockTransport(record))
    return seen


def _good(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/getMe"):
        return httpx.Response(200, json={"ok": True, "result": {"username": "haul_bot"}})
    if path.endswith("/setWebhook"):
        return httpx.Response(200, json={"ok": True, "result": True})
    if path.endswith("/getWebhookInfo"):
        return httpx.Response(200, json={"ok": True, "result": {"url": "https://haul.example.com/v1/telegram/webhook",
                                                                "pending_update_count": 2}})
    return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}], "usage": {}})


def _rows(world: World) -> list[str]:
    conn = open_connection(path=world.path)
    rows = [r[0] for r in conn.execute("select name from app_settings order by name")]
    conn.close()
    return rows


def _audit(world: World) -> str:
    conn = open_connection(path=world.path)
    rows = conn.execute("select action, outcome, detail_json from audit_log order by audit_id").fetchall()
    conn.close()
    return " ".join(f"{r[0]} {r[1]} {r[2]}" for r in rows)


def test_checks_save_nothing(world: World, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    seen = _use(world, _good)
    admin = world.client("admin")
    model = admin.post(f"{URL}/model/test", json={"model_id": "model-try", "api_key": "sk-try-KKKK"})
    assert model.json()["ok"] is True
    assert json.loads(seen[-1].content)["model"] == "model-try" and seen[-1].headers["authorization"] == "Bearer sk-try-KKKK"
    bot = admin.post(f"{URL}/telegram/test", json={"bot_token": TOKEN})
    assert bot.json() == {**bot.json(), "ok": True, "message": "@haul_bot"}
    assert _rows(world) == []
    assert "SETTINGS_TEST" in _audit(world)
    for secret in ("sk-try-KKKK", TOKEN):
        assert secret not in caplog.text + _audit(world) + model.text + bot.text


def test_provider_errors_are_shown_without_the_key(world: World) -> None:
    _use(world, lambda r: httpx.Response(401, json={"error": {"message": "Incorrect API key provided: sk-env"}}))
    r = world.client("admin").post(f"{URL}/model/test", json={})
    assert r.json()["ok"] is False and "Incorrect API key" in r.json()["message"] and "sk-env" not in r.text


def test_telegram_errors_never_contain_the_token(world: World) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    _use(world, boom)
    r = world.client("admin").post(f"{URL}/telegram/test", json={"bot_token": TOKEN})
    assert r.json()["ok"] is False and "ConnectError" in r.json()["message"] and TOKEN not in r.text


def test_a_saved_key_is_not_sent_to_another_provider(world: World) -> None:
    seen = _use(world, _good)
    r = world.client("admin").post(f"{URL}/model/test", json={"provider": "anthropic"})
    assert r.status_code == 422 and r.json()["reason_code"] == "KEY_REQUIRED_FOR_PROVIDER" and seen == []


def test_register_uses_the_public_url_and_marks_it_registered(world: World) -> None:
    seen = _use(world, _good)
    admin = world.client("admin")
    assert admin.post(f"{URL}/telegram", json={"bot_token": TOKEN, "webhook_secret": SECRET}).status_code == 200
    assert admin.get(URL).json()["webhook"] == "needs_registering"
    r = admin.post(f"{URL}/telegram/register-webhook", json={})
    assert r.json()["ok"] is True and "2 waiting" in r.json()["message"]
    sent = json.loads(next(q for q in seen if q.url.path.endswith("/setWebhook")).content)
    assert sent == {"url": "https://haul.example.com/v1/telegram/webhook", "secret_token": SECRET}
    assert admin.get(URL).json()["webhook"] == "registered"
    assert "TELEGRAM_WEBHOOK_REGISTER OK" in _audit(world) and SECRET not in _audit(world) + r.text
    admin.post(f"{URL}/telegram", json={"webhook_secret": "another-secret-0123456789"})
    assert admin.get(URL).json()["webhook"] == "needs_registering"


def test_a_refused_registration_is_shown_and_not_marked(world: World) -> None:
    _use(world, lambda r: httpx.Response(400, json={"ok": False, "description": f"bad webhook for {TOKEN} {SECRET}"}))
    admin = world.client("admin")
    admin.post(f"{URL}/telegram", json={"bot_token": TOKEN, "webhook_secret": SECRET})
    r = admin.post(f"{URL}/telegram/register-webhook", json={})
    assert r.json()["ok"] is False and "bad webhook" in r.json()["message"]
    assert TOKEN not in r.text and SECRET not in r.text
    assert admin.get(URL).json()["webhook"] == "needs_registering"


def test_register_without_a_public_url_is_refused(world: World) -> None:
    seen = _use(world, _good)
    world.deps.public_url = ""
    admin = world.client("admin")
    admin.post(f"{URL}/telegram", json={"bot_token": TOKEN, "webhook_secret": SECRET})
    r = admin.post(f"{URL}/telegram/register-webhook", json={})
    assert r.status_code == 422 and r.json()["reason_code"] == "PUBLIC_URL_NOT_SET" and seen == []
    assert admin.get(URL).json()["webhook"] == "no_public_url"


@pytest.mark.parametrize("short", ["short", "x" * 15])
def test_a_short_env_secret_is_never_reported_as_working(world: World, short: str) -> None:
    seen = _use(world, _good)
    world.deps.settings = SettingsSource({**BASE, "SETUHAUL_TELEGRAM_WEBHOOK_SECRET": short},
                                         transport=httpx.MockTransport(_good))
    admin = world.client("admin")
    # Saving only a token would leave the bot off, so it is refused and nothing is saved.
    r = admin.post(f"{URL}/telegram", json={"bot_token": TOKEN})
    assert r.status_code == 422 and r.json()["reason_code"] == "TELEGRAM_SECRET_TOO_SHORT"
    assert _rows(world) == []
    # Nor can the short secret be registered from a token already in .env.
    world.deps.settings = SettingsSource({**BASE, "SETUHAUL_TELEGRAM_BOT_TOKEN": TOKEN,
                                          "SETUHAUL_TELEGRAM_WEBHOOK_SECRET": short}, transport=httpx.MockTransport(_good))
    r = admin.post(f"{URL}/telegram/register-webhook", json={})
    assert r.status_code == 422 and r.json()["reason_code"] == "TELEGRAM_SECRET_TOO_SHORT" and seen == []
    assert admin.get(URL).json()["webhook"] == "needs_registering"
    # A new secret puts it right.
    assert admin.post(f"{URL}/telegram", json={"generate_secret": "yes"}).status_code == 200


def _hook(world: World, secret: str) -> int:
    return world.client().post("/v1/telegram/webhook", json={"update_id": 1},
                               headers={"X-Telegram-Bot-Api-Secret-Token": secret}).status_code


def _secret(world: World) -> str:
    conn = open_connection(path=world.path)
    assert world.deps.settings is not None
    value = world.deps.settings.effective(conn).env["SETUHAUL_TELEGRAM_WEBHOOK_SECRET"]
    conn.close()
    return value


def test_switching_to_another_bot_retires_the_old_webhook_secret(world: World) -> None:
    _use(world, _good)
    admin = world.client("admin")
    admin.post(f"{URL}/telegram", json={"bot_token": TOKEN, "webhook_secret": SECRET})
    # The same bot with a regenerated token keeps its secret.
    assert admin.post(f"{URL}/telegram", json={"bot_token": "123:regenerated"}).status_code == 200
    assert _secret(world) == SECRET
    # Another bot: the old one's calls must stop being accepted.
    assert admin.post(f"{URL}/telegram", json={"bot_token": "456:other-bot"}).status_code == 200
    assert _secret(world) != SECRET and len(_secret(world)) >= 32
    assert _hook(world, SECRET) == 401 and _hook(world, _secret(world)) == 200
    assert admin.get(URL).json()["webhook"] == "needs_registering"


def test_another_bot_cannot_be_given_the_old_secret_again(world: World) -> None:
    _use(world, _good)
    admin = world.client("admin")
    admin.post(f"{URL}/telegram", json={"bot_token": TOKEN, "webhook_secret": SECRET})
    r = admin.post(f"{URL}/telegram", json={"bot_token": "456:other-bot", "webhook_secret": SECRET})
    assert r.status_code == 422 and r.json()["reason_code"] == "TELEGRAM_NEW_SECRET_REQUIRED"
    assert _secret(world) == SECRET
    chosen = "a-different-secret-0123456789"
    assert admin.post(f"{URL}/telegram", json={"bot_token": "456:other-bot", "webhook_secret": chosen}).status_code == 200
    assert _secret(world) == chosen


def test_the_model_test_sends_a_tool_as_driver_messages_do(world: World) -> None:
    def refuses_tools(request: httpx.Request) -> httpx.Response:
        if "tools" in json.loads(request.content):
            return httpx.Response(400, json={"error": {"message": "Function tools need reasoning_effort none"}})
        return _good(request)

    seen = _use(world, refuses_tools)
    r = world.client("admin").post(f"{URL}/model/test", json={})
    assert json.loads(seen[-1].content)["tools"][0]["type"] == "function"
    assert r.json()["ok"] is False and "reasoning_effort" in r.json()["message"]
