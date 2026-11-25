"""The server and the password script refuse to run on the sample database, which must never change."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

from setuhaul import api
from setuhaul.config import REPO_ROOT

SEED = REPO_ROOT / "data" / "setuhaul.sqlite3"


@pytest.fixture()
def built(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    # The app itself is never built, so no test here can open or write the sample database.
    made: list[object] = []
    monkeypatch.setattr(api, "create_app", made.append)
    monkeypatch.setenv("SETUHAUL_REDIS_URL", "fakeredis://")
    monkeypatch.delenv("SETUHAUL_BOOTSTRAP_ADMIN_EMAIL", raising=False)
    return made


@pytest.mark.parametrize("setting", [None, "data/setuhaul.sqlite3", str(SEED), "data/../data/setuhaul.sqlite3"])
def test_the_server_does_not_start_on_the_sample_database(
        setting: str | None, built: list[object], monkeypatch: pytest.MonkeyPatch) -> None:
    if setting is None:
        monkeypatch.delenv("SETUHAUL_DB_PATH", raising=False)
    else:
        monkeypatch.setenv("SETUHAUL_DB_PATH", setting)
    with pytest.raises(SystemExit) as stopped:
        api.create_app_from_env()
    assert "SETUHAUL_DB_PATH" in str(stopped.value) and "working copy" in str(stopped.value)
    assert built == []


def test_the_server_starts_on_a_copy(tmp_path: Path, built: list[object], monkeypatch: pytest.MonkeyPatch) -> None:
    # Same file name, different place: only the sample file itself is refused.
    copy = tmp_path / "setuhaul.sqlite3"
    shutil.copyfile(SEED, copy)
    monkeypatch.setenv("SETUHAUL_DB_PATH", str(copy))
    api.create_app_from_env()
    assert len(built) == 1


def test_the_password_script_refuses_before_asking_for_a_password(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    spec = importlib.util.spec_from_file_location("set_admin_password", REPO_ROOT / "scripts" / "set_admin_password.py")
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    def never(prompt: str = "") -> str:
        raise AssertionError("asked for a password")

    monkeypatch.setattr(script.getpass, "getpass", never)
    monkeypatch.delenv("SETUHAUL_DB_PATH", raising=False)
    before = SEED.read_bytes()
    assert script.main(["admin@example.com"]) == 1
    assert "SETUHAUL_DB_PATH" in capsys.readouterr().err
    assert SEED.read_bytes() == before
