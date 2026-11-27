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
from tests.exception_helpers import D1, D2, booked_world, run
from tests.shipment_helpers import DAY, count, create, query, slot_at


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


# ---- warn, move and free --------------------------------------------------------------------------------
def preview(world: World, data: dock_blocks.BlockRequest) -> list[dock_blocks.PlanItem]:
    return list(run(world, dock_blocks.preview, actor=world.users["wh_a"], data=data))


def apply(world: World, data: dock_blocks.BlockRequest, expected: list[dock_blocks.Expected] | None = None,
          telegram_on: bool = False) -> dock_blocks.BlockResult:
    if expected is None:
        expected = [dock_blocks.Expected(appointment_id=i.appointment_id, new_slot_id=i.new_slot_id)
                    for i in preview(world, data) if i.action != "SKIP"]
    result: dock_blocks.BlockResult = run(world, dock_blocks.block, actor=world.users["wh_a"], data=data, expected=expected,
                                          telegram_on=telegram_on)
    return result


def row_of(world: World, appointment_id: str) -> tuple[Any, ...]:
    return tuple(query(world, "select appointment_status, is_current, cancellation_reason from appointments where appointment_id=?",
                       appointment_id)[0])


def unchanged(world: World, appointment_id: str) -> None:
    assert row_of(world, appointment_id) == ("CONFIRMED", 1, None)
    assert count(world, "dock_blocks") == 0 and count(world, "appointments", "booking_source='WAREHOUSE'") == 1


def test_preview_proposes_the_closest_slot_on_another_dock(tmp_path: Path) -> None:
    world, _, appt = booked_world(tmp_path)
    [item] = preview(world, request(9, 10, dock=D1))
    assert (item.action, item.new_dock_code, item.new_start) == ("MOVE", "D2", f"{DAY}T09:00:00+05:30")
    unchanged(world, appt)


def test_block_moves_the_booking(tmp_path: Path) -> None:
    world, shipment_id, appt = booked_world(tmp_path)
    apply(world, request(9, 10, dock=D1))
    assert row_of(world, appt) == ("CANCELLED", 0, "DOCK_BLOCKED")
    new = query(world, "select appointment_status, is_current, replaced_appointment_id, slot_id from appointments"
                       " where shipment_id=? and appointment_id<>?", shipment_id, appt)[0]
    assert tuple(new) == ("CONFIRMED", 1, appt, slot_at(world, 9, D2))
    notices = query(world, "select payload_json from notification_outbox where recipient_type='DRIVER'")
    assert len(notices) == 1 and "D2" in notices[0][0]


def test_two_shipments_never_get_the_same_slot(tmp_path: Path) -> None:
    world, _, _ = booked_world(tmp_path, to_time="11:00")
    create(world, slot_at(world, 10, D1), order_number="ORD-261127-002")
    create(world, slot_at(world, 10, D2), order_number="ORD-261127-003")
    result = apply(world, request(9, 11, dock=D1))
    assert sorted(i.action for i in result.items) == ["FREE", "MOVE"]


def test_nothing_fits_frees_and_keeps_the_shipment(tmp_path: Path) -> None:
    world, shipment_id, appt = booked_world(tmp_path, to_time="10:00")
    create(world, slot_at(world, 9, D2), order_number="ORD-261127-002")
    [item] = apply(world, request(9, 10, dock=D1)).items
    assert item.action == "FREE" and row_of(world, appt) == ("CANCELLED", 0, "DOCK_BLOCKED")
    assert query(world, "select current_status from shipments where shipment_id=?", shipment_id)[0][0] == "ASSIGNED"


def test_a_changed_plan_saves_nothing(tmp_path: Path) -> None:
    world, _, appt = booked_world(tmp_path)
    with pytest.raises(dock_blocks.PlanChanged) as caught:
        apply(world, request(9, 10, dock=D1), expected=[])
    assert [i.appointment_id for i in caught.value.items] == [appt]
    unchanged(world, appt)


def test_a_truck_on_site_is_listed_and_left(tmp_path: Path) -> None:
    world, shipment_id, appt = booked_world(tmp_path)
    query(world, "update shipments set current_status='AT_GATE' where shipment_id=?", shipment_id)
    [item] = apply(world, request(9, 10, dock=D1)).items
    assert (item.action, item.why) == ("SKIP", "TRUCK_ON_SITE")
    assert row_of(world, appt) == ("CONFIRMED", 1, None)


def test_a_failure_part_way_saves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world, _, appt = booked_world(tmp_path)

    def broken(*args: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(dock_blocks.passes, "sync_window", broken)
    with pytest.raises(RuntimeError):
        apply(world, request(9, 10, dock=D1))
    unchanged(world, appt)


def test_unreachable_drivers_are_named(tmp_path: Path) -> None:
    world, _, _ = booked_world(tmp_path)
    assert apply(world, request(9, 10, dock=D1)).unreachable == ["ORD-261127-001"]


def crews(world: World, number: int) -> None:
    query(world, "update labour_windows set available_crews=? where window_start like ?", number, f"{DAY}%")


def test_a_freed_booking_no_longer_takes_a_crew_when_the_next_one_is_placed(tmp_path: Path) -> None:
    world, _, _ = booked_world(tmp_path, to_time="11:00")
    # Placed first (higher priority), and nothing can take it, so it is only freed.
    unplaceable = create(world, slot_at(world, 10, D1), order_number="ORD-261127-002", priority_code="HIGH")
    create(world, slot_at(world, 9, D2), order_number="ORD-261127-003")
    query(world, "update shipments set expected_unload_min=600 where shipment_id=?", unplaceable)
    crews(world, 1)
    items = {i.order_reference: i for i in preview(world, request(9, 11, dock=D1))}
    assert items["ORD-261127-002"].action == "FREE"
    moved = items["ORD-261127-001"]
    assert (moved.action, moved.new_start) == ("MOVE", f"{DAY}T10:00:00+05:30")


def test_a_hold_the_block_releases_no_longer_takes_a_crew(tmp_path: Path) -> None:
    world, _, _ = booked_world(tmp_path, to_time="11:00")
    create(world, slot_at(world, 9, D2), order_number="ORD-261127-003")
    query(world, "insert into appointments(appointment_id, shipment_id, slot_id, appointment_status, booking_source, is_current,"
                 " booked_at, updated_at) values ('APT-HELD-T','SHP1001',?,'HELD','DRIVER_CHAT',0,?,?)",
          slot_at(world, 10, D1), world.clock.now().isoformat(), world.clock.now().isoformat())
    query(world, "insert into appointment_hold_meta(appointment_id, driver_id, thread_id, option_set_id, request_fingerprint,"
                 " expires_at, expires_epoch) values ('APT-HELD-T','DRV001','THR-T-NONE','OS-1','fp',?,?)",
          world.clock.now().isoformat(), int(world.clock.now().timestamp()) + 600)
    crews(world, 1)
    [item] = preview(world, request(9, 11, dock=D1))
    assert (item.action, item.new_start) == ("MOVE", f"{DAY}T10:00:00+05:30")
