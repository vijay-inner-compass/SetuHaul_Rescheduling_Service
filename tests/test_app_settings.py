"""Saved settings laid over the environment, with secrets encrypted."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Connection

import pytest
from cryptography.fernet import Fernet

from setuhaul import app_settings
from setuhaul.db import make_working_db, open_connection

KEY = Fernet.generate_key().decode()
NOW = datetime(2026, 11, 26, tzinfo=UTC)


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[tuple[Path, Connection]]:
    path = make_working_db(tmp_path / "s.sqlite3")
    conn = open_connection(path=path)
    yield path, conn
    conn.close()


def save(conn: Connection, values: dict[str, str], env: dict[str, str] | None = None) -> None:
    app_settings.save(conn, values, env=env if env is not None else {"SETUHAUL_SETTINGS_KEY": KEY}, updated_by="a@x.test", now=NOW)
    conn.commit()


def test_a_saved_value_wins_and_reset_returns_to_env(db: tuple[Path, Connection]) -> None:
    _, conn = db
    env = {"SETUHAUL_SETTINGS_KEY": KEY, "SETUHAUL_MODEL_ID": "a"}
    save(conn, {"SETUHAUL_MODEL_ID": "b"})
    got = app_settings.effective(conn, env)
    assert got.env["SETUHAUL_MODEL_ID"] == "b" and got.source["SETUHAUL_MODEL_ID"] == "saved"
    app_settings.reset(conn, ["SETUHAUL_MODEL_ID"])
    got = app_settings.effective(conn, env)
    assert got.env["SETUHAUL_MODEL_ID"] == "a" and got.source["SETUHAUL_MODEL_ID"] == "env"
    assert got.source["SETUHAUL_MODEL_PROVIDER"] == "unset"


def test_a_secret_is_not_in_the_database_in_clear(db: tuple[Path, Connection]) -> None:
    path, conn = db
    save(conn, {"SETUHAUL_MODEL_API_KEY": "sk-live-1234"})
    assert app_settings.effective(conn, {"SETUHAUL_SETTINGS_KEY": KEY}).env["SETUHAUL_MODEL_API_KEY"] == "sk-live-1234"
    for file in (path, path.with_name(path.name + "-wal")):
        assert not file.exists() or b"sk-live-1234" not in file.read_bytes()


@pytest.mark.parametrize("read_key", [Fernet.generate_key().decode(), "", "abc"])
def test_an_unreadable_secret_is_empty_not_env(db: tuple[Path, Connection], read_key: str) -> None:
    _, conn = db
    save(conn, {"SETUHAUL_MODEL_API_KEY": "sk-saved"})
    got = app_settings.effective(conn, {"SETUHAUL_SETTINGS_KEY": read_key, "SETUHAUL_MODEL_API_KEY": "sk-env"})
    assert got.env["SETUHAUL_MODEL_API_KEY"] == "" and got.unreadable == frozenset({"SETUHAUL_MODEL_API_KEY"})


@pytest.mark.parametrize(("env", "problem"), [({}, "missing"), ({"SETUHAUL_SETTINGS_KEY": "abc"}, "invalid")])
def test_master_key_problems(db: tuple[Path, Connection], env: dict[str, str], problem: str) -> None:
    _, conn = db
    assert app_settings.master_key_problem(env) == problem
    with pytest.raises(ValueError):
        save(conn, {"SETUHAUL_MODEL_API_KEY": "sk-x"}, env=env)
    save(conn, {"SETUHAUL_MODEL_ID": "m"}, env=env)
    assert app_settings.effective(conn, env).env["SETUHAUL_MODEL_ID"] == "m"
    assert app_settings.master_key_problem({"SETUHAUL_SETTINGS_KEY": KEY}) is None


def test_a_name_that_is_not_editable_is_refused(db: tuple[Path, Connection]) -> None:
    _, conn = db
    with pytest.raises(ValueError):
        save(conn, {"SETUHAUL_DB_PATH": "/tmp/other"})
    conn.execute("insert into app_settings values ('SETUHAUL_DB_PATH', '/tmp/other', 0, 'x', 'y')")
    assert "SETUHAUL_DB_PATH" not in app_settings.effective(conn, {}).env


def test_without_the_table_env_is_used(db: tuple[Path, Connection]) -> None:
    _, conn = db
    conn.execute("drop table app_settings")
    env = {"SETUHAUL_MODEL_ID": "a"}
    got = app_settings.effective(conn, env)
    assert got.env == env and got.source["SETUHAUL_MODEL_ID"] == "env" and not got.unreadable


def test_a_saved_empty_value_overrides_env(db: tuple[Path, Connection]) -> None:
    _, conn = db
    save(conn, {"SETUHAUL_OPENAI_REASONING_EFFORT": ""})
    got = app_settings.effective(conn, {"SETUHAUL_OPENAI_REASONING_EFFORT": "none"})
    assert got.env["SETUHAUL_OPENAI_REASONING_EFFORT"] == ""
