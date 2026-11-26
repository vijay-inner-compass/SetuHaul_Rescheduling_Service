"""The admin API that reads, saves and resets settings."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from setuhaul.db import open_connection
from setuhaul.runtime_settings import SettingsSource
from tests.auth_helpers import World, build_world
from tests.helpers import at
from tests.runtime_helpers import BASE

URL = "/v1/admin/settings"


@pytest.fixture()
def world(tmp_path: Path) -> World:
    from setuhaul.clock import FakeClock

    built = build_world(tmp_path, FakeClock(at("09:35")))
    built.deps.settings = SettingsSource(BASE)
    return built


def _in_force(world: World) -> dict[str, str]:
    conn = open_connection(path=world.path)
    assert world.deps.settings is not None
    env = world.deps.settings.effective(conn).env
    conn.close()
    return env


def _audit(world: World) -> str:
    conn = open_connection(path=world.path)
    rows = conn.execute("select action, target_id, detail_json from audit_log order by audit_id").fetchall()
    conn.close()
    return " ".join(f"{r[0]} {r[1]} {r[2]}" for r in rows)


def _ok(client: TestClient, section: str, body: dict[str, str]) -> None:
    r = client.post(f"{URL}/{section}", json=body)
    assert r.status_code == 200, r.text


def _refused(client: TestClient, section: str, body: dict[str, object], code: str, status: int = 422) -> None:
    r = client.post(f"{URL}/{section}", json=body)
    assert (r.status_code, r.json()["reason_code"]) == (status, code), r.text


def test_only_an_admin_from_this_site_can_read_or_change(world: World) -> None:
    assert world.client().get(URL).status_code == 401
    assert world.client("wh_a").get(URL).status_code == 403
    assert world.client("wh_a").post(f"{URL}/model", json={"model_id": "x"}).status_code == 403
    r = world.client("admin").post(f"{URL}/model", json={"model_id": "x"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert _in_force(world)["SETUHAUL_MODEL_ID"] == "model-env"


def test_no_secret_comes_back_or_reaches_the_audit_log(world: World, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    secrets_ = ["sk-secret-AAAA1111", "ls-secret-BBBB2222", "123:secret-CCCC3333", "hook-secret-DDDD4444"]
    admin = world.client("admin")
    seen = []
    for section, body in (("model", {"api_key": secrets_[0]}),
                          ("langsmith", {"api_key": secrets_[1]}),
                          ("telegram", {"bot_token": secrets_[2], "webhook_secret": secrets_[3]})):
        seen.append(admin.post(f"{URL}/{section}", json=body))
        assert seen[-1].status_code == 200
    seen.append(admin.get(URL))
    seen += [admin.post(f"{URL}/{s}/reset", json={}) for s in ("model", "langsmith", "telegram")]
    text = " ".join(r.text for r in seen) + _audit(world) + caplog.text
    for secret in secrets_:
        assert secret not in text
    values = seen[3].json()["settings"]
    assert values["SETUHAUL_MODEL_API_KEY"] == {"source": "saved", "set": True, "last4": "1111"}
    assert values["SETUHAUL_TELEGRAM_WEBHOOK_SECRET"] == {"source": "saved", "set": True}
    assert "SETTINGS_SAVE model" in _audit(world) and "SETTINGS_RESET telegram" in _audit(world)


def test_the_audit_log_shows_ordinary_changes(world: World) -> None:
    _ok(world.client("admin"), "model", {"model_id": "model-new"})
    assert '"SETUHAUL_MODEL_ID": {"from": "model-env", "to": "model-new"}' in _audit(world)


def test_a_pasted_key_is_stripped(world: World) -> None:
    _ok(world.client("admin"), "model", {"api_key": "  sk-x \n"})
    assert _in_force(world)["SETUHAUL_MODEL_API_KEY"] == "sk-x"


@pytest.mark.parametrize("price", ["abc", "-1", "nan", "inf"])
def test_bad_prices_are_refused(world: World, price: str) -> None:
    _refused(world.client("admin"), "model", {"model_id": "other", "price_in": price}, "INVALID_REQUEST")
    assert _in_force(world)["SETUHAUL_MODEL_ID"] == "model-env"


@pytest.mark.parametrize("body", [{"model_id": ""}, {"api_key": "two words"}, {"unknown": "x"}, {"model_id": 5},
                                  {"provider": "nobody"}])
def test_bad_input_is_refused_without_echoing_it(world: World, body: dict[str, object]) -> None:
    _refused(world.client("admin"), "model", body, "INVALID_REQUEST")
    assert "two words" not in world.client("admin").post(f"{URL}/model", json=body).text


def test_provider_change_rules(world: World) -> None:
    admin = world.client("admin")
    _refused(admin, "model", {"provider": "anthropic"}, "KEY_REQUIRED_FOR_PROVIDER")
    _ok(admin, "model", {"provider": "anthropic", "api_key": "sk-ant"})
    assert _in_force(world)["SETUHAUL_MODEL_API_KEY"] == "sk-ant"
    # Back to the provider named in .env: no key needed, and the other provider's key is dropped.
    _ok(admin, "model", {"provider": "openai"})
    assert (_in_force(world)["SETUHAUL_MODEL_PROVIDER"], _in_force(world)["SETUHAUL_MODEL_API_KEY"]) == ("openai", "sk-env")


def test_a_key_saved_alone_stays_with_its_provider(world: World) -> None:
    _ok(world.client("admin"), "model", {"api_key": "sk-openai-saved"})
    assert world.deps.settings is not None
    # .env is later changed to the other provider.
    world.deps.settings = SettingsSource({**BASE, "SETUHAUL_MODEL_PROVIDER": "anthropic"})
    assert _in_force(world)["SETUHAUL_MODEL_PROVIDER"] == "openai"


def test_an_env_key_is_never_sent_to_a_different_saved_provider(world: World) -> None:
    from setuhaul.model_gateway import UnconfiguredGateway

    # openai saved without a key, relying on .env; then .env moves to anthropic with an anthropic key.
    admin = world.client("admin")
    _ok(admin, "model", {"provider": "anthropic", "api_key": "sk-ant"})
    _ok(admin, "model", {"provider": "openai"})
    conn = open_connection(path=world.path)
    moved = SettingsSource({**BASE, "SETUHAUL_MODEL_PROVIDER": "anthropic", "SETUHAUL_MODEL_API_KEY": "sk-ant-env"})
    assert isinstance(moved.model(conn)[0], UnconfiguredGateway)
    conn.close()


def test_langsmith_needs_confirmation_and_hides_text_by_default(world: World) -> None:
    admin = world.client("admin")
    _refused(admin, "langsmith", {"enabled": "on"}, "CONFIRMATION_REQUIRED")
    _ok(admin, "langsmith", {"enabled": "on", "confirm": "SEND_TO_LANGSMITH", "api_key": "ls-k", "project": "live"})
    env = _in_force(world)
    assert (env["SETUHAUL_LANGSMITH_ENABLED"], env["SETUHAUL_LANGSMITH"], env["SETUHAUL_LANGSMITH_HIDE_TEXT"]) == ("true", "approved", "1")
    _ok(admin, "langsmith", {"hide_text": "off"})
    _ok(admin, "langsmith", {"enabled": "off"})
    env = _in_force(world)
    assert (env["SETUHAUL_LANGSMITH_ENABLED"], env["SETUHAUL_LANGSMITH_HIDE_TEXT"]) == ("false", "0")


def test_telegram_pair_and_generated_secret(world: World) -> None:
    admin = world.client("admin")
    _refused(admin, "telegram", {"bot_token": "123:abc"}, "TELEGRAM_PAIR_REQUIRED")
    _refused(admin, "telegram", {"bot_token": "123:abc/../x", "webhook_secret": "s" * 16}, "INVALID_REQUEST")
    _refused(admin, "telegram", {"bot_token": "123:abc", "webhook_secret": "short"}, "INVALID_REQUEST")
    _ok(admin, "telegram", {"bot_token": "123:abc", "generate_secret": "yes"})
    assert len(_in_force(world)["SETUHAUL_TELEGRAM_WEBHOOK_SECRET"]) >= 32


def test_without_a_master_key_ordinary_settings_still_save(world: World) -> None:
    world.deps.settings = SettingsSource({**BASE, "SETUHAUL_SETTINGS_KEY": "abc"})
    admin = world.client("admin")
    assert admin.get(URL).json()["master_key"] == "invalid"
    _refused(admin, "model", {"api_key": "sk-x"}, "SETTINGS_KEY_UNAVAILABLE")
    _ok(admin, "model", {"model_id": "model-new"})
    assert _in_force(world)["SETUHAUL_MODEL_ID"] == "model-new"


def test_reset_restores_env(world: World) -> None:
    admin = world.client("admin")
    _ok(admin, "model", {"model_id": "model-new"})
    assert admin.post(f"{URL}/model/reset", json={}).status_code == 200
    assert _in_force(world)["SETUHAUL_MODEL_ID"] == "model-env"


def test_without_a_settings_source_nothing_can_be_saved(world: World) -> None:
    world.deps.settings = None
    admin = world.client("admin")
    assert admin.get(URL).json()["live"] is False
    _refused(admin, "model", {"model_id": "x"}, "SETTINGS_NOT_AVAILABLE", 409)
    _refused(admin, "nonsense", {}, "UNKNOWN_SECTION", 404)
