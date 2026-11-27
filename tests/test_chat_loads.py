"""Which load each chat message is about, recorded when it is written."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from setuhaul import chat_loads, partner_booking
from setuhaul.clock import FakeClock
from setuhaul.config import load_settings
from setuhaul.db import make_working_db, open_connection
from setuhaul.gps import store_ping
from setuhaul.identity import provision_identities
from setuhaul.memory import ConversationMemoryStore
from setuhaul.position import handle_new_position
from setuhaul.schemas import GpsPing
from setuhaul.stub_model import StubGateway
from tests.auth_helpers import World, build_world
from tests.exception_helpers import booked_world
from tests.helpers import at

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


# ---- every writer records ---------------------------------------------------------------------------------------
def world_at(tmp_path: Path) -> World:
    world = build_world(tmp_path, FakeClock(at("09:35")))
    c = open_connection(path=world.path)
    provision_identities(c)
    c.commit()
    c.close()
    return world


def loads(world: World, where: str = "1=1") -> list[tuple[str, str]]:
    """(sender, load) of recorded messages written by the test (the supplied ones are left out)."""
    c = open_connection(path=world.path)
    rows = c.execute("select m.sender_type, l.shipment_id from chat_message_loads l join chat_messages m using(chat_message_id)"
                     f" where m.chat_message_id not like 'MSG0%' and m.chat_message_id not like 'MSG-T-%' and {where} order by m.rowid")
    out = [(r[0], r[1]) for r in rows]
    c.close()
    return out


def deliver(world: World, text: str, message_id: str, phone: str) -> Any:
    body = json.dumps({"from": phone, "message_id": message_id, "text": text}).encode()
    sig = hmac.new(world.deps.webhook_secret, body, hashlib.sha256).hexdigest()
    return TestClient(world.app).post("/v1/driver/messages", content=body, headers={"x-signature": sig})


def only_load(world: World, shipment_id: str = "SHP1006", driver: str = "DRV006") -> str:
    """Leave the driver with one unfinished load; return their phone."""
    c = open_connection(path=world.path)
    c.execute("update shipments set current_status='COMPLETED' where driver_id=? and shipment_id<>?", (driver, shipment_id))
    c.commit()
    phone = str(c.execute("select phone from drivers where driver_id=?", (driver,)).fetchone()[0])
    c.close()
    return phone


def test_driver_turn_is_recorded_once_even_if_delivered_twice(tmp_path: Path) -> None:
    world = world_at(tmp_path)
    phone = only_load(world)
    world.deps.gateway_factory = StubGateway
    first = deliver(world, "hello", "M-1", phone)
    assert first.status_code == 200, first.text
    assert loads(world) == [("DRIVER", "SHP1006"), ("AGENT", "SHP1006")]
    assert deliver(world, "hello", "M-1", phone).status_code == 200
    assert len(loads(world)) == 2


def test_recording_failure_does_not_fail_the_turn(tmp_path: Path) -> None:
    world = world_at(tmp_path)
    phone = only_load(world)
    world.deps.gateway_factory = StubGateway
    c = open_connection(path=world.path)
    c.execute("drop table chat_message_loads")
    c.commit()
    c.close()
    assert deliver(world, "hello", "M-2", phone).status_code == 200


def test_warehouse_notice_is_recorded(tmp_path: Path) -> None:
    world, shipment_id, appointment_id = booked_world(tmp_path)
    c = open_connection(path=world.path)
    c.execute("insert into chat_threads(thread_id, driver_id, opened_at, thread_status, thread_intent) values ('T','DRV001',?,'OPEN','UNKNOWN')",
              (world.clock.now().isoformat(),))
    c.commit()
    c.close()
    assert world.client("wh_a").post(f"/v1/warehouse/appointments/{appointment_id}/free", json={}).status_code == 200
    assert loads(world, "m.thread_id='T'") == [("OPERATIONS", shipment_id)]


def test_case_message_is_recorded(tmp_path: Path) -> None:
    world = world_at(tmp_path)
    case = world.cases["FAC-JAI-01"]
    r = world.client("wh_a").post(f"/v1/coordinator/cases/{case}/claim", json={})
    assert r.status_code == 200, r.text
    assert loads(world) == [("OPERATIONS", "SHP1001")]


def test_partner_answer_is_recorded(tmp_path: Path) -> None:
    world = world_at(tmp_path)
    c = open_connection(path=world.path)
    req = c.execute("select * from partner_requests where request_ref='PR-T-FAC-JAI-01'").fetchone()
    partner_booking._tell_driver(c, req, "DECLINED", world.clock.now())
    c.commit()
    c.close()
    assert loads(world) == [("OPERATIONS", "SHP1001")]


def test_offer_from_position_is_recorded(tmp_path: Path) -> None:
    world = world_at(tmp_path)
    c = open_connection(path=world.path)
    memory = ConversationMemoryStore(c, world.deps.redis, world.clock, load_settings())
    ship = c.execute("select s.shipment_id, f.lat, f.lon from shipments s join facilities f on f.facility_id=s.destination_facility_id"
                     " where s.shipment_id='SHP1006'").fetchone()
    store_ping(c, "SHP1006", GpsPing(lat=ship["lat"] + 0.2, lon=ship["lon"], recorded_at=world.clock.now()), world.clock, "TELEMATICS")
    c.commit()
    handle_new_position(c, "SHP1006", world.clock, memory)
    c.close()
    assert loads(world) and {load for _, load in loads(world)} == {"SHP1006"}


def test_a_record_goes_with_its_message(conn: sqlite3.Connection) -> None:
    with chat_loads.recording(conn, "T", "SHP1006"):
        say(conn, "X1")
    # The demo clears the chat log; the records must not block that.
    conn.execute("delete from chat_messages")
    assert conn.execute("select count(*) from chat_message_loads").fetchone()[0] == 0
