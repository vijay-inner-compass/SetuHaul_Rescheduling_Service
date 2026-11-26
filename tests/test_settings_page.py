"""The Configuration tab on the admin screen."""

from __future__ import annotations

from pathlib import Path

import pytest

from setuhaul.runtime_settings import SettingsSource
from tests.auth_helpers import World, build_world
from tests.helpers import at
from tests.runtime_helpers import BASE


@pytest.fixture()
def world(tmp_path: Path) -> World:
    from setuhaul.clock import FakeClock

    built = build_world(tmp_path, FakeClock(at("09:35")))
    built.deps.settings = SettingsSource({**BASE, "SETUHAUL_MODEL_API_KEY": "sk-env-secret-9999"})
    return built


def _page(world: World) -> str:
    text = world.client("admin").get("/admin").text
    return text[text.index('<section id="config"'):]


def test_admin_sees_the_tab_with_sources(world: World) -> None:
    full = world.client("admin").get("/admin").text
    assert 'data-tab="config"' in full and "settings.js" in full
    page = _page(world)
    assert 'value="model-env"' in page and ".env" in page and "not set" in page
    world.client("admin").post("/v1/admin/settings/model", json={"model_id": "model-saved"})
    page = _page(world)
    assert 'value="model-saved"' in page and "saved here" in page


def test_no_secret_is_in_the_page(world: World) -> None:
    admin = world.client("admin")
    admin.post("/v1/admin/settings/telegram", json={"bot_token": "123:token-SECRETAAAA", "webhook_secret": "hook-SECRETBBBB-0123"})
    admin.post("/v1/admin/settings/langsmith", json={"api_key": "ls-SECRETCCCC"})
    text = admin.get("/admin").text
    for secret in ("sk-env-secret", "token-SECRET", "hook-SECRET", "ls-SECRET"):
        assert secret not in text
    assert "ends in 9999" in text and "ends in CCCC" in text


def test_a_saved_value_is_escaped(world: World) -> None:
    world.client("admin").post("/v1/admin/settings/model", json={"model_id": '"><script>x</script>'})
    assert "<script>x" not in _page(world)


def test_key_fields_are_disabled_with_a_bad_master_key(world: World) -> None:
    world.deps.settings = SettingsSource({**BASE, "SETUHAUL_SETTINGS_KEY": "abc"})
    page = _page(world)
    assert "SETUHAUL_SETTINGS_KEY in .env is not a valid key." in page
    assert page.count('type="password"') == page.count('type="password" autocomplete="new-password" disabled') > 0


def test_an_unreadable_key_is_reported(world: World) -> None:
    world.client("admin").post("/v1/admin/settings/model", json={"api_key": "sk-saved"})
    from cryptography.fernet import Fernet

    world.deps.settings = SettingsSource({**BASE, "SETUHAUL_SETTINGS_KEY": Fernet.generate_key().decode()})
    assert "A saved key cannot be read. Enter it again or reset to .env." in _page(world)


def test_without_a_settings_source_the_tab_is_read_only(world: World) -> None:
    world.deps.settings = None
    page = _page(world)
    assert "Changes are for the live service." in page and "data-settings=" not in page


def test_a_database_without_the_table_still_shows_the_screen(world: World) -> None:
    import sqlite3

    with sqlite3.connect(world.path) as conn:
        conn.execute("drop table app_settings")
    assert 'value="model-env"' in _page(world)


def test_settings_js_is_served(world: World) -> None:
    r = world.client().get("/app/assets/settings.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]


def test_the_hide_text_choice_never_claims_hiding_that_is_not_happening(world: World) -> None:
    def drawn() -> str:
        page = _page(world)
        return page[page.index('name="hide_text"'):].split("</select>")[0]

    # Off and unset: hiding is the starting choice.
    assert '<option value="on" selected>' in drawn()
    world.deps.settings = SettingsSource({**BASE, "SETUHAUL_LANGSMITH_ENABLED": "true", "SETUHAUL_LANGSMITH": "approved",
                                          "LANGSMITH_API_KEY": "ls-env"})
    # Tracing from .env without hiding: say so.
    assert '<option value="off" selected>' in drawn()
