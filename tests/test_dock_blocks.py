"""Blocking a dock: storing it, ending it, and what it does to the bookings on it."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from setuhaul import dock_blocks
from setuhaul.board import build_board
from setuhaul.clock import FakeClock
from setuhaul.db import immediate, open_connection
from setuhaul.db import parse_operational_timestamp as ts
from setuhaul.identity import AuthError
from tests.auth_helpers import JAIPUR, World, build_world
from tests.exception_helpers import D2, booked_world, run
from tests.shipment_helpers import DAY, query


def request(hour_from: int, hour_to: int | None, dock: str = D2, **over: Any) -> dock_blocks.BlockRequest:
    values: dict[str, Any] = {
        "facility_id": JAIPUR, "dock_id": dock, "event_type": "MAINTENANCE", "reason": "Leveller repair",
        "starts": f"{DAY}T{hour_from:02d}:00", "ends": None if hour_to is None else f"{DAY}T{hour_to:02d}:00"} | over
    return dock_blocks.BlockRequest(**values)


def insert(world: World, data: dock_blocks.BlockRequest, who: str = "wh_a") -> str:
    conn = open_connection(path=world.path)
    try:
        with immediate(conn):
            return dock_blocks.insert_block_in_txn(conn, world.users[who], data, world.clock)
    finally:
        conn.close()


def cells(world: World, dock_code: str = "D2") -> dict[int, str]:
    conn = open_connection(path=world.path)
    try:
        board = build_board(conn, JAIPUR, world.clock.now())
    finally:
        conn.close()
    assert board is not None
    dock = next(d for d in board["docks"] if d["code"] == dock_code)
    return {s["hour"]: s["status"] for s in dock["slots"] if s["start_ts"].startswith(DAY)}


def reopen(world: World, event_id: str, who: str = "wh_a") -> None:
    run(world, dock_blocks.reopen, actor=world.users[who], dock_event_id=event_id)


def code_of(fn: Any, *args: Any) -> str:
    with pytest.raises(AuthError) as caught:
        fn(*args)
    return caught.value.code


def test_a_block_makes_the_slot_blocked(tmp_path: Path) -> None:
    world, _, _ = booked_world(tmp_path)
    insert(world, request(10, 12))
    assert [cells(world)[h] for h in (10, 11, 12)] == ["BLOCKED", "BLOCKED", "AVAILABLE"]


def test_reopening_one_of_two_blocks_keeps_the_other(tmp_path: Path) -> None:
    world, _, _ = booked_world(tmp_path)
    first = insert(world, request(10, 12))
    insert(world, request(11, 13))
    reopen(world, first)
    assert [cells(world)[h] for h in (10, 11, 12)] == ["AVAILABLE", "BLOCKED", "BLOCKED"]
    assert not query(world, "select 1 from dock_status_events where event_type='REOPENED'")


def test_reopening_a_future_open_ended_block_removes_it(tmp_path: Path) -> None:
    world, _, _ = booked_world(tmp_path)
    event_id = insert(world, request(10, None))
    reopen(world, event_id)
    assert cells(world)[12] == "AVAILABLE"
    assert code_of(reopen, world, event_id) == "BLOCK_NOT_FOUND"


def test_form_times_without_offset_are_india_time(tmp_path: Path) -> None:
    world, _, _ = booked_world(tmp_path)
    event_id = insert(world, request(10, 12))
    stored = query(world, "select event_start_ts from dock_status_events where dock_event_id=?", event_id)[0][0]
    assert stored == f"{DAY}T10:00:00+05:30"


def test_end_must_be_after_start_and_now(tmp_path: Path) -> None:
    world, _, _ = booked_world(tmp_path)
    assert code_of(insert, world, request(12, 12)) == "INVALID_TIMES"
    past = request(10, 12, starts="2026-11-20T10:00", ends="2026-11-20T12:00")
    assert code_of(insert, world, past) == "INVALID_TIMES"
    assert code_of(insert, world, request(10, 12, reason="   ")) == "REASON_REQUIRED"
    assert code_of(insert, world, request(10, 12, dock="DOCK-GGN-D1")) == "DOCK_NOT_FOUND"


def test_other_warehouse_cannot_block_or_reopen(tmp_path: Path) -> None:
    world, _, _ = booked_world(tmp_path)
    assert code_of(insert, world, request(10, 12), "wh_b") == "FACILITY_NOT_FOUND"
    event_id = insert(world, request(10, 12))
    assert code_of(reopen, world, event_id, "wh_b") == "BLOCK_NOT_FOUND"


def test_reopening_the_sample_breakdown_opens_its_slots(tmp_path: Path) -> None:
    world = build_world(tmp_path, FakeClock(ts("2026-08-04T09:35:00+05:30")))
    before = tuple(query(world, "select * from dock_status_events where dock_event_id='DEVT001'")[0])
    conn = open_connection(path=world.path)
    try:
        listed = dock_blocks.current(conn, [JAIPUR], world.clock.now())
    finally:
        conn.close()
    assert "DEVT001" in [b["dock_event_id"] for b in listed]
    reopen(world, "DEVT001")
    slots = query(world, "select slot_status, block_reason from appointment_slots where slot_id in"
                         " ('SLOT-JAI-031','SLOT-JAI-032','SLOT-JAI-033')")
    assert [tuple(s) for s in slots] == [("OPEN", None)] * 3
    assert tuple(query(world, "select * from dock_status_events where dock_event_id='DEVT001'")[0]) == before
    assert isinstance(datetime.fromisoformat(listed[0]["starts"]), datetime)
