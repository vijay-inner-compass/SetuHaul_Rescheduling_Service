"""SQLite connection factory and the operational timestamp contract."""

# Lazy annotations.
from __future__ import annotations

# shutil.copyfile copies the seed database to make a working copy.
import shutil

# sqlite3 is the database driver.
import sqlite3

# Iterator is the return type of a generator-based context manager.
from collections.abc import Iterator

# contextmanager turns the generator in immediate() into a "with" block.
from contextlib import contextmanager

# datetime is the parsed timestamp type.
from datetime import datetime

# Path for file locations.
from pathlib import Path

# ZoneInfo is the standard-library timezone database lookup.
from zoneinfo import ZoneInfo

# Settings supply the default database path and the busy timeout.
from setuhaul.config import load_settings

# The single operational timezone. Every time shown or compared in the service is in IST.
IST = ZoneInfo("Asia/Kolkata")


# A dedicated error type so callers can tell "bad timestamp" apart from other ValueErrors.
class TimestampError(ValueError):
    """Raised for naive, malformed or empty operational timestamps."""


def parse_operational_timestamp(raw: str) -> datetime:
    """Parse an ISO-8601 timestamp with an explicit offset into an aware IST datetime.

    Naive strings are rejected: a naive value cannot be compared safely and must never be
    silently assumed to be IST.
    """
    # Empty, blank or non-string input is rejected first, with the offending value shown for diagnosis.
    if not isinstance(raw, str) or not raw.strip():
        raise TimestampError(f"empty timestamp: {raw!r}")
    # Ignore surrounding whitespace.
    text = raw.strip()
    # A trailing "Z" means UTC; older Python versions do not accept "Z", so spell it as "+00:00".
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # A bare date ("2026-08-04") has no time and no offset, so it is not a usable timestamp.
    if "T" not in text:
        raise TimestampError(f"not an ISO-8601 date-time: {raw!r}")
    # Let the standard parser do the real work; wrap its error in our own type.
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TimestampError(f"invalid timestamp: {raw!r}") from exc
    # No offset means the time zone is unknown. We refuse to guess it.
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise TimestampError(f"naive timestamp (no offset): {raw!r}")
    # Convert to IST so every parsed value is comparable and displays in operational time.
    return dt.astimezone(IST)


def open_connection(path: Path | None = None, readonly: bool = False) -> sqlite3.Connection:
    """Open one connection (callers use one per worker).

    Read-only connections use the URI ``mode=ro`` and cannot write. Writable connections
    enable WAL, foreign keys and the documented busy timeout.
    """
    # Settings give the default path and the busy timeout.
    settings = load_settings()
    # An explicit path wins; otherwise use the configured database.
    db_path = Path(path) if path is not None else settings.db_path
    # SQLite would silently create an empty database for a missing file; fail loudly instead.
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    if readonly:
        # mode=ro makes writes impossible at the driver level, which is how the audit scripts prove they never modify data.
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    else:
        # timeout is in seconds: how long a writer waits for another writer's lock before giving up.
        conn = sqlite3.connect(db_path, timeout=settings.sqlite_busy_timeout_ms / 1000)
        # WAL lets readers and one writer work at the same time and is crash-safe.
        conn.execute("pragma journal_mode=wal")
    # Rows can then be read by column name (row["driver_id"]) as well as by position.
    conn.row_factory = sqlite3.Row
    # The same busy timeout, applied at the SQL level.
    conn.execute(f"pragma busy_timeout={settings.sqlite_busy_timeout_ms}")
    # SQLite does not enforce foreign keys unless asked, per connection.
    conn.execute("pragma foreign_keys=on")
    return conn


# Where the numbered .sql migration files live (repo_root/migrations).
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def apply_migrations(conn: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply unapplied ``NNN_*.sql`` files in filename order; record each in schema_migrations."""
    # A bookkeeping table remembers which migration files have run, so re-running is harmless.
    conn.execute(
        "create table if not exists schema_migrations "
        "(filename text primary key, applied_at text not null)"
    )
    # The set of file names already applied.
    done = {r[0] for r in conn.execute("select filename from schema_migrations")}
    # What this call applied, returned so callers and tests can see it.
    applied: list[str] = []
    # Sorted by name: the numeric prefix (001_, 002_ ...) defines the order.
    for path in sorted(directory.glob("*.sql")):
        # Skip anything already applied.
        if path.name in done:
            continue
        # Read the migration script as text.
        script = path.read_text(encoding="utf-8")
        # The bookkeeping insert runs inside the same transaction as the script, so a half-applied migration is impossible.
        stamp = f"insert into schema_migrations values ('{path.name}', datetime('now'));"
        # PRAGMA foreign_keys is a no-op inside a transaction, so toggle it around the script.
        conn.execute("pragma foreign_keys=off")
        try:
            # begin ... commit: the whole script and its stamp succeed together or not at all.
            conn.executescript(f"begin;\n{script}\n{stamp}\ncommit;")
            # After the change, verify no row now points at a missing parent.
            violations = conn.execute("pragma foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(f"{path.name} left foreign key violations: {violations}")
        finally:
            # Always restore foreign-key enforcement, even if the migration failed.
            conn.execute("pragma foreign_keys=on")
        applied.append(path.name)
    return applied


def make_working_db(dest: Path, source: Path | None = None) -> Path:
    """Copy the pristine seed to ``dest`` and migrate the copy. The seed itself is never written."""
    # The source is the pristine seed database unless a test passes another.
    src = Path(source) if source is not None else load_settings().db_path
    # Copy first, so every change below happens on the copy.
    shutil.copyfile(src, dest)
    # Open the copy and bring its schema up to date.
    conn = open_connection(path=dest)
    try:
        apply_migrations(conn)
    finally:
        # Close even if a migration failed, so the file is not left locked.
        conn.close()
    return dest


@contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` ... commit, rolling back on any exception.

    The write lock is taken up front, so a busy database fails here (``database is locked``)
    rather than part-way through. Callers classify that separately from business conflicts.
    """
    conn.commit()  # never nest inside an implicit transaction
    # IMMEDIATE takes the single write lock now. Two requests racing for one slot therefore queue up here,
    # and the second sees the first one's committed result: this is what makes "first to commit wins" safe.
    conn.execute("begin immediate")
    try:
        # The caller's "with" body runs here, holding the lock.
        yield conn
    except BaseException:
        # Any failure (even KeyboardInterrupt) undoes everything the body did, then re-raises to the caller.
        conn.rollback()
        raise
    else:
        # The body finished cleanly: make all its changes permanent in one step.
        conn.commit()
