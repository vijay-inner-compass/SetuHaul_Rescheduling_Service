"""Freeing a booked slot from the warehouse side: the step every exception action shares."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from setuhaul import release
from setuhaul.db import open_connection
from setuhaul.identity import AuthError
from tests.exception_helpers import booked_world, run
from tests.shipment_helpers import count, query


def _refused(world, who: str, appointment_id: str) -> str:
    with pytest.raises(AuthError) as caught:
        run(world, release.free_slot, actor=world.users[who], appointment_id=appointment_id)
    return caught.value.code


def test_free_slot_cancels_the_booking_and_keeps_the_shipment(tmp_path: Path) -> None:
    world, shipment_id, appt = booked_world(tmp_path)
    found = run(world, release.free_slot, actor=world.users["wh_a"], appointment_id=appt)
    row = query(world, "select appointment_status, is_current, cancellation_reason from appointments where appointment_id=?", appt)[0]
    assert tuple(row) == ("CANCELLED", 0, "SLOT_FREED") and found.shipment_id == shipment_id
    assert query(world, "select current_status from shipments where shipment_id=?", shipment_id)[0][0] == "ASSIGNED"
    assert count(world, "appointment_events", f"appointment_id='{appt}' and event_type='APPOINTMENT_CANCELLED'") == 1
    assert count(world, "audit_log", "action='BOOKING_FREED'") == 1


def test_freeing_twice_is_refused_and_changes_nothing(tmp_path: Path) -> None:
    world, _, appt = booked_world(tmp_path)
    run(world, release.free_slot, actor=world.users["wh_a"], appointment_id=appt)
    assert _refused(world, "wh_a", appt) == "NOT_BOOKED"
    assert count(world, "appointment_events", f"appointment_id='{appt}' and event_type='APPOINTMENT_CANCELLED'") == 1


def test_another_warehouse_cannot_see_the_booking(tmp_path: Path) -> None:
    world, _, appt = booked_world(tmp_path)
    assert _refused(world, "wh_b", appt) == "APPOINTMENT_NOT_FOUND"


def test_a_started_slot_is_refused(tmp_path: Path) -> None:
    world, _, appt = booked_world(tmp_path)
    # 10:00 on the 27th to 09:05 on the 28th: truck has not arrived, so a missed/started slot can be freed.
    world.clock.advance(timedelta(hours=23, minutes=5))
    found = run(world, release.free_slot, actor=world.users["wh_a"], appointment_id=appt)
    assert found.appointment_id == appt


def test_unloading_and_on_site_are_refused(tmp_path: Path) -> None:
    world, shipment_id, appt = booked_world(tmp_path)
    query(world, "update shipments set current_status='AT_GATE' where shipment_id=?", shipment_id)
    assert _refused(world, "wh_a", appt) == "TRUCK_ON_SITE"
    query(world, "update appointments set appointment_status='IN_PROGRESS' where appointment_id=?", appt)
    assert _refused(world, "wh_a", appt) == "UNLOADING"


def test_tell_driver_queues_a_notice_and_reports_reachability(tmp_path: Path) -> None:
    world, _, appt = booked_world(tmp_path)
    found = run(world, release.free_slot, actor=world.users["wh_a"], appointment_id=appt)
    conn = open_connection(path=world.path)
    try:
        reached = release.tell_driver(conn, world.clock, False, found, "slot_freed", order=found.order_reference,
                                      dock=found.dock_code, start="09:00", date="28 Nov 2026")
    finally:
        conn.close()
    assert reached is False
    assert count(world, "notification_outbox", "recipient_type='DRIVER'") == 1
