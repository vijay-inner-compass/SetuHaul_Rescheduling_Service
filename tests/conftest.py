"""Shared fixtures. The seed database under data/ is never written; tests use migrated copies."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import fakeredis
import pytest

from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection
from setuhaul.db import parse_operational_timestamp as ts

# Tests must never reach a real outside service. The developer's .env holds real keys (model, tracing, mail, sign-in), and the
# mail settings would copy all of it into this process, so file loading is switched off and anything already in the shell is
# removed. Set here, at import, so it also covers subprocesses and code that runs before any fixture.
os.environ["SETUHAUL_ENV_FILE"] = ""
os.environ["SETUHAUL_DOTENV"] = "off"
_OUTSIDE = ("LANGSMITH_", "LANGCHAIN_", "SETUHAUL_LANGSMITH", "SETUHAUL_MODEL_", "SETUHAUL_SMTP_", "SETUHAUL_IMAP_",
            "SETUHAUL_TELEGRAM_", "SETUHAUL_GOOGLE_", "SETUHAUL_PARTNER_EMAIL_MODE")
for _name in [n for n in os.environ if n.startswith(_OUTSIDE)]:
    del os.environ[_name]


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock(ts("2026-08-04T09:35:00+05:30"))


@pytest.fixture()
def working_db_path(tmp_path: Path) -> Path:
    return make_working_db(tmp_path / "working.sqlite3")


@pytest.fixture()
def db(working_db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_connection(path=working_db_path)
    yield conn
    conn.close()


@pytest.fixture()
def redis_client() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(decode_responses=True)
