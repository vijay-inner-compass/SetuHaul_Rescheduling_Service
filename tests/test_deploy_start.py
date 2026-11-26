"""The container's start-up step: make the working database once, migrate it, never overwrite it."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from setuhaul.db import open_connection

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "data" / "setuhaul.sqlite3"


def load_start() -> ModuleType:
    spec = importlib.util.spec_from_file_location("deploy_start", ROOT / "deploy" / "start.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def count(db: Path, table: str) -> int:
    conn = open_connection(path=db)
    try:
        return int(conn.execute(f"select count(*) from {table}").fetchone()[0])
    finally:
        conn.close()


def test_first_start_makes_a_migrated_database_with_driver_phones_and_leaves_the_seed_alone(tmp_path: Path) -> None:
    before = SEED.read_bytes()
    db = tmp_path / "setuhaul.db"
    load_start().prepare(db, SEED)
    assert count(db, "schema_migrations") == len(list((ROOT / "migrations").glob("*.sql")))
    assert count(db, "channel_identities") > 0
    assert SEED.read_bytes() == before


def test_a_later_start_keeps_what_is_already_in_the_database(tmp_path: Path) -> None:
    db = tmp_path / "setuhaul.db"
    start = load_start()
    start.prepare(db, SEED)
    conn = open_connection(path=db)
    conn.execute("delete from channel_identities")
    conn.execute("create table kept (x)")
    conn.commit()
    conn.close()
    start.prepare(db, SEED)
    assert count(db, "kept") == 0  # the table is still there: the file was not copied over
    assert count(db, "channel_identities") > 0  # provisioning runs on every start
