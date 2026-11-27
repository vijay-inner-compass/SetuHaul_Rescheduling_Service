"""An assigned shipment stays hidden from every driver-facing path until the driver confirms it."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import fakeredis
import pytest

from setuhaul import cancellation, facility_change, partner_booking
from setuhaul.clock import FakeClock
from setuhaul.gps import open_shipments_for_driver, store_ping
from setuhaul.identity import AuthError, _context_for, authorize_shipment
from setuhaul.memory import ConversationMemoryStore
from setuhaul.option_history import record_option_set
from setuhaul.position import handle_new_position
from setuhaul.schemas import GpsPing
from setuhaul.tools import _has_partner_shipment, _list_my_shipments
from tests.shipment_helpers import confirm_row, mark_unconfirmed

SHIP, DRIVER = "SHP1006", "DRV006"


def _listed(db: sqlite3.Connection) -> list[str]:
    ctx = SimpleNamespace(conn=db, driver_id=DRIVER)
    return [s["shipment_id"] for s in _list_my_shipments(ctx, None)["shipments"]]  # type: ignore[arg-type]


def _memory(db: sqlite3.Connection, clock: FakeClock, redis_client: fakeredis.FakeRedis) -> ConversationMemoryStore:
    return ConversationMemoryStore(db, redis_client, clock)


def test_hidden_from_lists_and_ownership(db: sqlite3.Connection, clock: FakeClock, redis_client: fakeredis.FakeRedis) -> None:
    mark_unconfirmed(db, SHIP)
    assert SHIP not in _listed(db)
    assert SHIP not in open_shipments_for_driver(db, DRIVER)
    with pytest.raises(AuthError, match="FOREIGN_SHIPMENT"):
        authorize_shipment(db, _context_for(db, DRIVER), SHIP)
    memory = _memory(db, clock, redis_client)
    with pytest.raises(AuthError, match="FOREIGN_SHIPMENT"):
        memory.open_thread(DRIVER, SHIP)
    assert SHIP not in memory._active_shipments(DRIVER)


def test_refused_by_every_change(db: sqlite3.Connection, clock: FakeClock, redis_client: fakeredis.FakeRedis) -> None:
    mark_unconfirmed(db, SHIP)
    thread = _memory(db, clock, redis_client).open_thread(DRIVER)
    with pytest.raises(AuthError, match="FOREIGN_SHIPMENT"):
        cancellation.cancel_pending_change(db, DRIVER, thread, SHIP, clock)
    assert facility_change.request_change(db, clock, driver_id=DRIVER, shipment_id=SHIP, to_facility_id="FAC-JAI-01").status == "NOT_MOVABLE"
    with pytest.raises(AuthError, match="FOREIGN_SHIPMENT"):
        record_option_set(db, SimpleNamespace(driver_id=DRIVER, shipment_id=SHIP), thread, [], None, clock)  # type: ignore[arg-type]


def test_hidden_from_the_partner_paths(db: sqlite3.Connection, clock: FakeClock) -> None:
    db.execute("update facilities set is_partner=1 where facility_id=(select destination_facility_id from shipments where shipment_id=?)", (SHIP,))
    db.execute("update shipments set current_status='IN_TRANSIT' where driver_id=?", (DRIVER,))
    db.execute("update shipments set current_status='CANCELLED' where driver_id=? and shipment_id<>?", (DRIVER, SHIP))
    db.commit()
    ctx = SimpleNamespace(conn=db, driver_id=DRIVER)
    assert _has_partner_shipment(ctx)  # type: ignore[arg-type]
    mark_unconfirmed(db, SHIP)
    assert not _has_partner_shipment(ctx)  # type: ignore[arg-type]
    start = clock.now() + timedelta(hours=3)
    got = partner_booking.request_slot(db, clock, shipment_id=SHIP, proposed_start=start, proposed_end=start + timedelta(hours=1))
    assert got.status == "SHIPMENT_NOT_RESCHEDULABLE"


def test_a_position_says_nothing_about_it(db: sqlite3.Connection, clock: FakeClock, redis_client: fakeredis.FakeRedis) -> None:
    memory = _memory(db, clock, redis_client)
    memory.open_thread(DRIVER)
    fac = db.execute("select f.lat, f.lon from shipments s join facilities f on f.facility_id=s.destination_facility_id "
                     "where s.shipment_id=?", (SHIP,)).fetchone()
    store_ping(db, SHIP, GpsPing(lat=fac["lat"] + 20 / 111.2, lon=fac["lon"], recorded_at=clock.now()), clock, "TEST")
    db.commit()
    count = "select count(*) from chat_messages"
    before = db.execute(count).fetchone()[0]
    mark_unconfirmed(db, SHIP)
    handle_new_position(db, SHIP, clock, memory)
    assert db.execute(count).fetchone()[0] == before
    # The same ping speaks once the driver has confirmed, so the silence above was the rule and not a missing fixture.
    confirm_row(db, SHIP)
    handle_new_position(db, SHIP, clock, memory)
    assert db.execute(count).fetchone()[0] > before


def test_a_shipment_without_a_row_behaves_as_before(db: sqlite3.Connection) -> None:
    assert SHIP in _listed(db)
    authorize_shipment(db, _context_for(db, DRIVER), SHIP)


def test_a_confirmed_row_is_visible(db: sqlite3.Connection) -> None:
    mark_unconfirmed(db, SHIP)
    confirm_row(db, SHIP)
    assert SHIP in _listed(db)
    authorize_shipment(db, _context_for(db, DRIVER), SHIP)


def test_a_thread_does_not_keep_an_unconfirmed_shipment_selected(db: sqlite3.Connection, clock: FakeClock,
                                                                 redis_client: fakeredis.FakeRedis) -> None:
    memory = _memory(db, clock, redis_client)
    thread = memory.open_thread(DRIVER, SHIP)
    assert memory.load(DRIVER, thread).state.selected_shipment_id == SHIP
    mark_unconfirmed(db, SHIP)
    assert memory.load(DRIVER, thread).state.selected_shipment_id is None
