"""Records which load a chat message is about, so a warehouse sees the messages about its own loads."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

_log = logging.getLogger(__name__)


def mark(conn: sqlite3.Connection) -> int:
    """Where the chat log ends now; `record` covers what is written after it."""
    return int(conn.execute("select coalesce(max(rowid), 0) from chat_messages").fetchone()[0])


def record(conn: sqlite3.Connection, thread_id: str, after: int, shipment_id: str | None) -> None:
    """Record the thread's newer messages against the load, with its destination now. An existing record is kept.

    The caller owns the transaction. No load, nothing recorded: such messages are visible to administrators only.
    """
    if shipment_id is None:
        return
    conn.execute(
        "insert or ignore into chat_message_loads(chat_message_id, shipment_id, facility_id) "
        "select m.chat_message_id, s.shipment_id, s.destination_facility_id from chat_messages m "
        "join shipments s on s.shipment_id=? where m.thread_id=? and m.rowid>?", (shipment_id, thread_id, after))


def load_of_turn(conn: sqlite3.Connection, driver_id: str, selected: str | None) -> str | None:
    """The load a driver's message and its reply are about: the selected one while unfinished, else their only one.

    Never guessed between two.
    """
    open_loads = [r[0] for r in conn.execute(
        "select shipment_id from shipments where driver_id=? and current_status not in ('COMPLETED','CANCELLED')", (driver_id,))]
    if selected in open_loads:
        return selected
    return str(open_loads[0]) if len(open_loads) == 1 else None


@contextmanager
def recording(conn: sqlite3.Connection, thread_id: str | None, shipment_id: str | None) -> Iterator[None]:
    """Record whatever the block writes into the thread against the load. Never fails the writing itself.

    Inside the caller's transaction the record joins it; otherwise it is committed here.
    """
    outer = conn.in_transaction
    after = None
    try:
        after = mark(conn)
    except sqlite3.Error:
        _log.warning("chat message load not recorded", exc_info=True)
    yield
    if after is None or thread_id is None or shipment_id is None:
        return
    try:
        record(conn, thread_id, after, shipment_id)
        if not outer:
            conn.commit()
    except sqlite3.Error:
        _log.warning("chat message load not recorded", exc_info=True)
