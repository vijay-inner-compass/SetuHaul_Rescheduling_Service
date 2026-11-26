"""Saved model settings apply from the next driver message."""

from __future__ import annotations

from setuhaul.db import open_connection
from setuhaul.runtime_settings import SettingsSource
from tests.auth_helpers import add_user, sign_in
from tests.runtime_helpers import BASE, Provider, save
from tests.test_api import env, msg, post  # noqa: F401 - env is a fixture


def _live(deps, provider: Provider, base: dict[str, str] = BASE) -> None:  # type: ignore[no-untyped-def]
    deps.settings = SettingsSource(base, transport=provider.transport)


def test_a_saved_model_id_is_used_by_the_next_message(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    deps, client, _, _, path = env
    provider = Provider()
    _live(deps, provider)
    assert post(client, msg("hello", mid="A")).status_code == 200
    save(path, {"SETUHAUL_MODEL_ID": "model-saved"})
    assert post(client, msg("hello again", mid="B")).status_code == 200
    assert [b["model"] for b in provider.bodies()] == ["model-env", "model-saved"]


def test_saved_key_prices_and_reasoning_effort_reach_the_call(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    deps, client, _, _, path = env
    provider = Provider()
    _live(deps, provider)
    save(path, {"SETUHAUL_MODEL_API_KEY": "sk-saved", "SETUHAUL_OPENAI_REASONING_EFFORT": "none",
                "SETUHAUL_MODEL_PRICE_IN_PER_MTOK": "2", "SETUHAUL_MODEL_PRICE_OUT_PER_MTOK": "3"})
    post(client, msg("hello"))
    assert provider.requests[0].headers["authorization"] == "Bearer sk-saved"
    assert provider.bodies()[0]["reasoning_effort"] == "none"
    conn = open_connection(path=path)
    cost = conn.execute("select cost_usd from model_calls").fetchone()[0]
    conn.close()
    assert round(cost, 6) == 0.005


def test_an_unreadable_key_takes_the_safe_outage_path(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    deps, client, _, _, path = env
    provider = Provider()
    save(path, {"SETUHAUL_MODEL_API_KEY": "sk-saved"})
    # The master key changed since the save; the environment still has a key that must not be used.
    _live(deps, provider, {**BASE, "SETUHAUL_SETTINGS_KEY": ""})
    r = post(client, msg("hello"))
    assert r.status_code == 200 and provider.requests == [] and r.json()["case_id"]


def test_an_unknown_provider_takes_the_safe_outage_path(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    deps, client, *_ = env
    provider = Provider()
    _live(deps, provider, {**BASE, "SETUHAUL_MODEL_PROVIDER": "nobody"})
    r = post(client, msg("hello"))
    assert r.status_code == 200 and provider.requests == []


def test_the_model_tab_shows_what_is_in_force(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    deps, client, _, clock, path = env
    _live(deps, Provider())
    save(path, {"SETUHAUL_MODEL_ID": "model-saved", "SETUHAUL_MODEL_PRICE_IN_PER_MTOK": "2.5"})
    conn = open_connection(path=path)
    sign_in(client, conn, add_user(conn, clock, "boss@x.test", "ADMIN"), clock)
    conn.close()
    text = client.get("/admin").text
    assert "openai / model-saved" in text and "USD 2.5" in text


def test_without_a_settings_source_nothing_changes(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    deps, client, calls, _, path = env
    save(path, {"SETUHAUL_MODEL_ID": "model-saved"})
    assert deps.settings is None
    post(client, msg("my truck is 20 minutes late"))
    assert calls.n == 1
