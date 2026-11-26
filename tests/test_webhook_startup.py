"""Opt-in registration of the Telegram webhook when the service starts."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from setuhaul import api
from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from setuhaul.runtime_settings import SettingsSource
from tests.auth_helpers import World, build_world
from tests.helpers import at
from tests.runtime_helpers import BASE, save

TOKEN, SECRET = "123:token-ZZZZ", "webhook-secret-0123456789"
PAIR = {"SETUHAUL_TELEGRAM_BOT_TOKEN": TOKEN, "SETUHAUL_TELEGRAM_WEBHOOK_SECRET": SECRET}


@pytest.fixture()
def world(tmp_path: Path) -> World:
    built = build_world(tmp_path, FakeClock(at("09:35")))
    built.deps.public_url = "https://haul.example.com"
    built.deps.auto_register_webhook = True
    return built


def _telegram_ok(request: httpx.Request) -> httpx.Response:
    result: object = True
    if request.url.path.endswith("/getWebhookInfo"):
        result = {"url": "https://haul.example.com/v1/telegram/webhook", "pending_update_count": 0}
    return httpx.Response(200, json={"ok": True, "result": result})


def _start(world: World, env: dict[str, str] | None = None,
           handler: Callable[[httpx.Request], httpx.Response] = _telegram_ok) -> list[httpx.Request]:
    """Start the service and stop it again; return what it sent to Telegram."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        if request.url.path.rsplit("/", 1)[-1] in ("setWebhook", "getWebhookInfo"):
            seen.append(request)
        return handler(request)

    world.deps.settings = SettingsSource({**BASE, **(PAIR if env is None else env)}, transport=httpx.MockTransport(record))
    # Leaving the block waits for the start-up work to finish.
    with TestClient(api.create_app(world.deps)):
        pass
    return seen


def _webhook_state(world: World) -> str:
    return str(world.client("admin").get("/v1/admin/settings").json()["webhook"])


def _audit(world: World) -> list[tuple[str | None, str]]:
    conn = open_connection(path=world.path)
    rows = [(r[0], r[1]) for r in
            conn.execute("select actor_email, outcome from audit_log where action='TELEGRAM_WEBHOOK_REGISTER'")]
    conn.close()
    return rows


def test_start_registers_the_public_address(world: World) -> None:
    seen = _start(world)
    assert [r.url.path for r in seen] == [f"/bot{TOKEN}/setWebhook", f"/bot{TOKEN}/getWebhookInfo"]
    body = seen[0].content.decode()
    assert "https://haul.example.com/v1/telegram/webhook" in body and SECRET in body
    assert _webhook_state(world) == "registered"
    assert _audit(world) == [(None, "OK")]


def test_saved_settings_win_over_env(world: World) -> None:
    save(world.path, {"SETUHAUL_TELEGRAM_BOT_TOKEN": "456:saved", "SETUHAUL_TELEGRAM_WEBHOOK_SECRET": SECRET})
    seen = _start(world)
    assert seen[0].url.path == "/bot456:saved/setWebhook"


def test_nothing_is_sent_unless_switched_on(world: World) -> None:
    world.deps.auto_register_webhook = False
    assert _start(world) == []
    assert _audit(world) == []


@pytest.mark.parametrize("public_url, env", [
    ("", PAIR),
    ("https://haul.example.com", {}),
    ("https://haul.example.com", {**PAIR, "SETUHAUL_TELEGRAM_WEBHOOK_SECRET": "short"}),
])
def test_nothing_is_sent_when_something_is_missing(world: World, caplog: pytest.LogCaptureFixture, public_url: str,
                                                   env: dict[str, str]) -> None:
    world.deps.public_url = public_url
    assert _start(world, env) == []
    assert "Telegram webhook not registered" in caplog.text


def test_a_refusal_is_logged_without_secrets_and_the_service_still_runs(world: World, caplog: pytest.LogCaptureFixture) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": f"bad {TOKEN} {SECRET}"})

    with caplog.at_level(logging.INFO):
        _start(world, handler=refuse)
    assert "Telegram refused" in caplog.text
    assert TOKEN not in caplog.text and SECRET not in caplog.text
    assert _audit(world) == [(None, "FAILED")]
    assert _webhook_state(world) == "needs_registering"


@pytest.mark.parametrize("value, expected", [("1", True), ("", False), ("0", False)])
def test_the_switch_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str,
                                                 expected: bool) -> None:
    built = build_world(tmp_path, FakeClock(at("09:35")))
    seen: list[api.AppDeps] = []
    monkeypatch.setenv("SETUHAUL_DB_PATH", str(built.path))
    monkeypatch.setenv("SETUHAUL_REDIS_URL", "fakeredis://")
    monkeypatch.setenv("SETUHAUL_TELEGRAM_AUTO_REGISTER", value)
    monkeypatch.setattr(api, "create_app", seen.append)
    api.create_app_from_env()
    assert seen[0].auto_register_webhook is expected
