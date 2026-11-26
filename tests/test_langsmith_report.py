"""The LangSmith panel: read only, quiet when it cannot read, and only on request. No network."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from setuhaul import langsmith_report
from setuhaul.clock import FakeClock
from setuhaul.db import parse_operational_timestamp
from tests.auth_helpers import World, build_world

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
ON = {"SETUHAUL_LANGSMITH_ENABLED": "true", "SETUHAUL_LANGSMITH": "approved", "LANGSMITH_API_KEY": "ls_test",
      "LANGSMITH_PROJECT": "p"}


@pytest.fixture(autouse=True)
def _no_cache() -> None:
    langsmith_report._cache.clear()


class Client:
    def __init__(self, runs: list[Any] | None = None, error: Exception | None = None) -> None:
        self.runs, self.error = runs or [], error
        self.built: list[dict[str, Any]] = []
        self.asked: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Client:
        self.built.append(kwargs)
        return self

    def list_runs(self, **kwargs: Any) -> Any:
        self.asked.append(kwargs)
        if self.error:
            raise self.error
        return iter(self.runs)


def _run(seconds: float, ago: float = 60, error: str | None = None) -> Any:
    start = NOW - timedelta(seconds=ago)
    return SimpleNamespace(start_time=start, end_time=start + timedelta(seconds=seconds), error=error, status="success")


def _load(client: Client, env: dict[str, str] = ON, at: float = 0.0) -> dict[str, Any]:
    return langsmith_report.load_summary(env, client, now=lambda: NOW, timer=lambda: at)


def test_off_builds_no_client() -> None:
    client = Client()
    result = _load(client, {})
    assert result["state"] == "off" and "switched off" in result["message"] and client.built == []


def test_summary_from_runs() -> None:
    result = _load(Client([_run(1, ago=300), _run(3, ago=30), _run(2, ago=90, error="boom")]))
    assert (result["state"], result["runs"], result["failed"], result["p50_ms"], result["p95_ms"]) == ("ok", 3, 1, 2000, 3000)
    assert result["latest"] == (NOW - timedelta(seconds=30)).isoformat() and result["capped"] is False


def test_asks_for_no_inputs_or_outputs() -> None:
    client = Client()
    _load(client)
    asked = client.asked[0]
    assert asked["select"] == ["start_time", "end_time", "error", "status"]
    assert (asked["project_name"], asked["is_root"], asked["limit"]) == ("p", True, 500)
    assert client.built[0]["api_key"] == "ls_test" and client.built[0]["timeout_ms"] == 5000


def test_window_is_real_time() -> None:
    client = Client()
    _load(client)
    assert client.asked[0]["start_time"] == NOW - timedelta(hours=24)


def test_a_full_page_is_marked_capped() -> None:
    assert _load(Client([_run(1)] * 500))["capped"] is True


def test_errors_are_quiet() -> None:
    result = _load(Client(error=TimeoutError("key ls_secret")))
    assert result["state"] == "error" and result["message"] == "Could not read from LangSmith (TimeoutError)."
    assert "ls_secret" not in str(result)


def test_a_read_that_drags_on_is_given_up() -> None:
    ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0])
    result = langsmith_report.load_summary(ON, Client([_run(1)] * 3), now=lambda: NOW, timer=lambda: next(ticks))
    assert result["state"] == "error"


def test_cached_for_sixty_seconds() -> None:
    client = Client()
    _load(client, at=0)
    _load(client, at=59)
    assert len(client.asked) == 1
    _load(client, at=61)
    assert len(client.asked) == 2


@pytest.fixture()
def world(tmp_path: Path) -> World:
    return build_world(tmp_path, FakeClock(parse_operational_timestamp("2026-08-04T09:35:00+05:30")))


def test_route_is_admin_only(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SETUHAUL_LANGSMITH_ENABLED", raising=False)
    url = "/v1/admin/model/langsmith"
    ok = world.client("admin").get(url)
    assert ok.status_code == 200 and ok.json()["state"] == "off"
    assert world.client("wh_a").get(url).status_code == 403
    assert world.client().get(url).status_code == 401


def test_the_panel_is_on_the_model_tab(world: World) -> None:
    text = world.client("admin").get("/admin").text
    assert "Load from LangSmith" in text and "/app/assets/model.js" in text
    assert "no reliable token or cost figures" in text
