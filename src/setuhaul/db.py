"""SQLite connection factory and the operational timestamp contract."""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from setuhaul.config import load_settings

IST = ZoneInfo("Asia/Kolkata")


class TimestampError(ValueError):
    """Raised for naive, malformed or empty operational timestamps."""


def parse_operational_timestamp(raw: str) -> datetime:
    """Parse an ISO-8601 timestamp with an explicit offset into an aware IST datetime.

    Naive strings are rejected: a naive value cannot be compared safely and must never be
    silently assumed to be IST.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise TimestampError(f"empty timestamp: {raw!r}")
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    if "T" not in text:
        raise TimestampError(f"not an ISO-8601 date-time: {raw!r}")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TimestampError(f"invalid timestamp: {raw!r}") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise TimestampError(f"naive timestamp (no offset): {raw!r}")
    return dt.astimezone(IST)


def open_connection(path: Path | None = None, readonly: bool = False) -> sqlite3.Connection:
    """Open one connection (callers use one per worker).

    Read-only connections use the URI ``mode=ro`` and cannot write. Writable connections
    enable WAL, foreign keys and the documented busy timeout.
    """
    settings = load_settings()
    db_path = Path(path) if path is not None else settings.db_path
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    if readonly:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(db_path, timeout=settings.sqlite_busy_timeout_ms / 1000)
        conn.execute("pragma journal_mode=wal")
    conn.row_factory = sqlite3.Row
    conn.execute(f"pragma busy_timeout={settings.sqlite_busy_timeout_ms}")
    conn.execute("pragma foreign_keys=on")
    return conn


MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def apply_migrations(conn: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply unapplied ``NNN_*.sql`` files in filename order; record each in schema_migrations."""
    conn.execute(
        "create table if not exists schema_migrations "
        "(filename text primary key, applied_at text not null)"
    )
    done = {r[0] for r in conn.execute("select filename from schema_migrations")}
    applied: list[str] = []
    for path in sorted(directory.glob("*.sql")):
        if path.name in done:
            continue
        script = path.read_text(encoding="utf-8")
        stamp = f"insert into schema_migrations values ('{path.name}', datetime('now'));"
        conn.executescript(f"begin;\n{script}\n{stamp}\ncommit;")
        applied.append(path.name)
    return applied


def make_working_db(dest: Path, source: Path | None = None) -> Path:
    """Copy the pristine seed to ``dest`` and migrate the copy. The seed itself is never written."""
    src = Path(source) if source is not None else load_settings().db_path
    shutil.copyfile(src, dest)
    conn = open_connection(path=dest)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    return dest
