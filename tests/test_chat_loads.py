"""Which load each chat message is about, recorded when it is written."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from setuhaul import chat_loads
from setuhaul.db import make_working_db, open_connection

NOW = "2026-08-04T09:35:00+05:30"


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = open_connection(path=make_working_db(tmp_path / "w.sqlite3"))
    # DRV006 has two unfinished loads in the sample data: one to Jaipur, one to Gurugram.
    c.execute("insert into chat_threads(thread_id, driver_id, opened_at, thread_status, thread_intent) values ('T','DRV006',?,'OPEN','UNKNOWN')",
              (NOW,))
    c.commit()
    yield c
    c.close()


def say(conn: sqlite3.Connection, ident: str, thread: str = "T") -> None:
    conn.execute("insert into chat_messages(chat_message_id, thread_id, sender_type, message_text, message_ts) values (?,?,'DRIVER','hi',?)",
                 (ident, thread, NOW))


def recorded(conn: sqlite3.Connection, prefix: str = "X") -> list[tuple[Any, ...]]:
    return [tuple(r) for r in conn.execute("select chat_message_id, shipment_id, facility_id from chat_message_loads"
                                           " where chat_message_id like ? order by 1", (prefix + "%",))]


def test_record_covers_only_new_messages(conn: sqlite3.Connection) -> None:
    say(conn, "X1")
    after = chat_loads.mark(conn)
    say(conn, "X2")
    say(conn, "X3")
    chat_loads.record(conn, "T", after, "SHP1006")
    chat_loads.record(conn, "T", after, "SHP1021")  # already recorded: the first record stays
    assert recorded(conn) == [("X2", "SHP1006", "FAC-JAI-01"), ("X3", "SHP1006", "FAC-JAI-01")]


def test_two_loads_and_nothing_selected_records_nothing(conn: sqlite3.Connection) -> None:
    assert chat_loads.load_of_turn(conn, "DRV006", None) is None
    after = chat_loads.mark(conn)
    say(conn, "X1")
    chat_loads.record(conn, "T", after, None)
    assert recorded(conn) == []


def test_selected_load_wins_while_unfinished(conn: sqlite3.Connection) -> None:
    assert chat_loads.load_of_turn(conn, "DRV006", "SHP1021") == "SHP1021"
    # Someone else's load is never taken.
    assert chat_loads.load_of_turn(conn, "DRV006", "SHP1001") is None
    conn.execute("update shipments set current_status='COMPLETED' where shipment_id='SHP1021'")
    # The finished one is dropped, leaving a single unfinished load.
    assert chat_loads.load_of_turn(conn, "DRV006", "SHP1021") == "SHP1006"


def test_facility_is_fixed_when_written(conn: sqlite3.Connection) -> None:
    after = chat_loads.mark(conn)
    say(conn, "X1")
    chat_loads.record(conn, "T", after, "SHP1006")
    conn.execute("update shipments set destination_facility_id='FAC-GGN-01', current_status='CANCELLED' where shipment_id='SHP1006'")
    assert recorded(conn) == [("X1", "SHP1006", "FAC-JAI-01")]


def test_recording_wraps_writes_and_never_raises(conn: sqlite3.Connection) -> None:
    with chat_loads.recording(conn, "T", "SHP1006"):
        say(conn, "X1")
    assert recorded(conn) == [("X1", "SHP1006", "FAC-JAI-01")] and not conn.in_transaction
    conn.execute("drop table chat_message_loads")
    with chat_loads.recording(conn, "T", "SHP1006"):
        say(conn, "X2")
    assert conn.execute("select 1 from chat_messages where chat_message_id='X2'").fetchone()


def test_migration_records_named_threads(conn: sqlite3.Connection) -> None:
    # Supplied threads that name a shipment are recorded against it; threads that name none are not.
    named = conn.execute("select count(*) from chat_messages m join chat_threads t using(thread_id) where t.shipment_id is not null").fetchone()[0]
    assert named > 0 and conn.execute("select count(*) from chat_message_loads").fetchone()[0] == named
