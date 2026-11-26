"""Container start-up: make the working database on the first start, then migrate it and map driver phones."""

from __future__ import annotations

import os
from pathlib import Path

from setuhaul.db import apply_migrations, make_working_db, open_connection
from setuhaul.identity import provision_identities

# The image's copy of the sample data; it is only ever read.
SEED = Path("/app/data/setuhaul.sqlite3")


def prepare(db: Path, seed: Path) -> None:
    """Safe to run on every start: an existing database is never copied over."""
    if not db.exists():
        # Build under another name and rename, so a start that dies part-way is not mistaken for a database.
        partial = db.with_name(db.name + ".partial")
        make_working_db(partial, seed)
        partial.replace(db)
    conn = open_connection(path=db)
    try:
        apply_migrations(conn)
        provision_identities(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    prepare(Path(os.environ["SETUHAUL_DB_PATH"]), SEED)
