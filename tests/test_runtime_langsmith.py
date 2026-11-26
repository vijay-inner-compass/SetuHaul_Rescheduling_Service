"""LangSmith follows the saved settings."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from setuhaul import langsmith_trace
from setuhaul.db import open_connection
from setuhaul.runtime_settings import SettingsSource
from tests.auth_helpers import add_user, sign_in
from tests.runtime_helpers import BASE, Provider, save
from tests.test_api import env, msg, post  # noqa: F401 - env is a fixture

ON = {"SETUHAUL_LANGSMITH_ENABLED": "true", "SETUHAUL_LANGSMITH": "approved", "LANGSMITH_API_KEY": "ls-saved",
      "LANGSMITH_PROJECT": "live"}


def _admin(client, path, clock) -> None:  # type: ignore[no-untyped-def]
    conn = open_connection(path=path)
    sign_in(client, conn, add_user(conn, clock, "boss@x.test", "ADMIN"), clock)
    conn.close()


def test_the_turn_traces_with_the_settings_in_force(env, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    deps, client, _, _, path = env
    seen: list[Mapping[str, str] | None] = []
    monkeypatch.setattr(langsmith_trace, "build_tracer", lambda env=None: seen.append(env))
    deps.settings = SettingsSource(BASE, transport=Provider().transport)
    save(path, ON)
    assert post(client, msg("hello")).status_code == 200
    assert seen and seen[-1] is not None
    assert (seen[-1]["LANGSMITH_API_KEY"], seen[-1]["LANGSMITH_PROJECT"]) == ("ls-saved", "live")


def test_saved_off_overrides_env_on(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    _deps, *_, path = env
    source = SettingsSource({**BASE, **ON})
    save(path, {"SETUHAUL_LANGSMITH_ENABLED": "false"})
    conn = open_connection(path=path)
    assert langsmith_trace.tracing_status(source.effective(conn).env).enabled is False
    conn.close()


def test_the_panel_and_limits_use_the_settings_in_force(env, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    deps, client, _, clock, path = env
    seen: list[Mapping[str, str] | None] = []
    monkeypatch.setattr("setuhaul.auth.admin_routes.load_summary", lambda env=None: seen.append(env) or {"state": "off"})
    deps.settings = SettingsSource(BASE)
    save(path, ON)
    _admin(client, path, clock)
    assert client.get("/v1/admin/model/langsmith").status_code == 200
    assert seen[0] is not None and seen[0]["LANGSMITH_API_KEY"] == "ls-saved"
    assert "<td>enabled</td>" in client.get("/admin").text
