"""Warehouses open all day and night, slots created automatically, and a dock board that shows one day."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from setuhaul import round_clock
from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from setuhaul.db import parse_operational_timestamp as ts
from tests.auth_helpers import World, build_world
from tests.exception_helpers import D1, D2, booked_world
from tests.shipment_helpers import DAY, NOW, count, create, open_av, query, slot_at

JAI = "FAC-JAI-01"
URL = "/v1/warehouse/auto-slots"


def all_day_world(tmp_path: Path) -> World:
    world = build_world(tmp_path, FakeClock(ts(NOW)))
    conn = open_connection(path=world.path)
    try:
        round_clock.open_all_hours(conn)
    finally:
        conn.close()
    return world


def slots_on(world: World, day: str, dock: str = D1) -> list[str]:
    return [r[0][11:16] for r in query(world, "select slot_start_ts from appointment_slots where dock_id=? and slot_start_ts like ?"
                                              " order by slot_start_ts", dock, f"{day}%")]


def test_every_warehouse_opens_all_day_with_no_last_start(tmp_path: Path) -> None:
    world = all_day_world(tmp_path)
    assert {(r[0], r[1]) for r in query(world, "select open_time, close_time from facilities")} == {("00:00", "00:00")}
    assert count(world, "facility_rules", "rule_type='LAST_NEW_START_TIME' and active_flag=1") == 0


def test_the_last_hour_of_the_day_can_be_opened_and_booked(tmp_path: Path) -> None:
    world = all_day_world(tmp_path)
    open_av(world, from_time="22:00", to_time="00:00")
    assert slots_on(world, DAY) == ["22:00", "23:00"]
    assert create(world, slot_at(world, 23))


def test_no_slots_appear_until_a_warehouse_has_a_crew_number(tmp_path: Path) -> None:
    world = all_day_world(tmp_path)
    before = count(world, "appointment_slots")
    conn = open_connection(path=world.path)
    try:
        assert round_clock.top_up(conn, world.clock) == 0
    finally:
        conn.close()
    assert count(world, "appointment_slots") == before


def test_saving_the_crew_number_creates_fourteen_days_of_slots(tmp_path: Path) -> None:
    world = all_day_world(tmp_path)
    r = world.client("wh_a").post(URL, json={"facility_id": JAI, "crews": 4})
    assert r.status_code == 200, r.text
    assert r.json()["slots_created"] > 0
    # Today only from the next hour (it is 10:00), then whole days.
    assert slots_on(world, "2026-11-27")[0] == "11:00"
    assert len(slots_on(world, DAY)) == 24 and len(slots_on(world, DAY, D2)) == 24
    assert len(slots_on(world, "2026-12-10")) == 24 and slots_on(world, "2026-12-11") == []
    assert {r[0] for r in query(world, "select available_crews from labour_windows where facility_id=? and source='AUTO'", JAI)} == {4}
    assert create(world, slot_at(world, 3))
    assert 'name="crews"' in world.client("wh_a").get(f"/warehouse?facility={JAI}").text


def test_the_next_day_adds_one_more_day_and_nothing_twice(tmp_path: Path) -> None:
    world = all_day_world(tmp_path)
    world.client("wh_a").post(URL, json={"facility_id": JAI, "crews": 4})
    world.clock.advance(timedelta(days=1))
    conn = open_connection(path=world.path)
    try:
        assert round_clock.top_up(conn, world.clock) > 0
        assert round_clock.top_up(conn, world.clock) == 0
    finally:
        conn.close()
    assert len(slots_on(world, "2026-12-11")) == 24 and len(slots_on(world, "2026-12-10")) == 24


def test_labour_already_recorded_is_kept_and_the_rest_of_the_day_filled(tmp_path: Path) -> None:
    world = all_day_world(tmp_path)
    open_av(world, crews=1)
    world.client("wh_a").post(URL, json={"facility_id": JAI, "crews": 4})
    assert len(slots_on(world, DAY)) == 24
    crews = {(r[0][11:16], r[1]) for r in query(world, "select window_start, available_crews from labour_windows"
                                                       " where facility_id=? and window_start like ?", JAI, f"{DAY}%")}
    assert crews == {("00:00", 4), ("09:00", 1), ("13:00", 4)}


def test_a_changed_crew_number_applies_to_the_automatic_windows(tmp_path: Path) -> None:
    world = all_day_world(tmp_path)
    client = world.client("wh_a")
    client.post(URL, json={"facility_id": JAI, "crews": 4})
    assert client.post(URL, json={"facility_id": JAI, "crews": 2}).status_code == 200
    assert {r[0] for r in query(world, "select available_crews from labour_windows where source='AUTO'")} == {2}


def test_another_warehouse_cannot_set_the_crew_number(tmp_path: Path) -> None:
    world = all_day_world(tmp_path)
    r = world.client("wh_b").post(URL, json={"facility_id": JAI, "crews": 4})
    assert r.status_code == 404 and count(world, "facility_crews") == 0


def test_the_dock_board_shows_one_day(tmp_path: Path) -> None:
    world, _, _ = booked_world(tmp_path)
    client = world.client("wh_a")
    board = lambda url: client.get(url).text.split('<table class="board">')[1].split("</table>")[0]
    # Booked for tomorrow: not on today's board, shown when tomorrow is asked for.
    page = client.get(f"/warehouse?facility={JAI}&day=2026-11-29").text
    assert 'name="day"' in page and "ORD-261127-001" not in page.split("</table>")[0]
    assert "ORD-261127-001" in board(f"/warehouse?facility={JAI}&day={DAY}")
    assert client.get(f"/warehouse?facility={JAI}&day=nonsense").status_code == 200
