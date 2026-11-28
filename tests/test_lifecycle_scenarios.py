"""End-to-end Driver + Warehouse Authority scenarios for allocation, warehouse change, and cancellation."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from setuhaul import facility_change, pass_check
from setuhaul.cancellation import cancel_confirmed_appointment
from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from setuhaul.db import parse_operational_timestamp as ts
from setuhaul.escalation import InMemoryEscalationSink
from tests.auth_helpers import World, build_world
from tests.exception_helpers import D1, appointment_of, booked_world
from tests.shipment_helpers import DAY, NOW, create, open_av, query, slot_at


@pytest.fixture()
def world(tmp_path: Path) -> World:
    return build_world(tmp_path, FakeClock(ts(NOW)))


def test_scenario_1_unconfirmed_warehouse_change_does_not_lock_driver_pass(world: World) -> None:
    """Changing warehouse on an unconfirmed shipment revokes old pass without locking driver out."""
    open_av(world)
    sid = create(world, slot_at(world, 12))
    assert world.client("wh_a").post(f"/v1/warehouse/shipments/{sid}/warehouse", json={"facility_id": "FAC-GGN-01"}).status_code == 200
    conn = open_connection(path=world.path)
    try:
        assert not pass_check.has_waiting(conn, "DRV001")
        assert pass_check.prompt_key(conn, "DRV001") is None
        # Even if driver replies with an old pass code, it does not increment failed_attempts.
        assert pass_check.check(conn, world.clock, driver_id="DRV001", thread_id="THR-1", text="ABCD-1234",
                                lang="en", sink=InMemoryEscalationSink()) is None
    finally:
        conn.close()
    assert query(world, "select failed_attempts, locked_at from shipment_assignments where shipment_id=?", sid)[0][0] == 0


def test_scenario_2_allocate_slot_filters_dock_type_and_warns_on_replace(world: World) -> None:
    """Shipments tab filters compatible docks for REEFER loads, hides partner warehouses, and warns before replacing."""
    open_av(world, dock_ids=["DOCK-JAI-D1", "DOCK-JAI-D5"], from_time="08:00", to_time="22:00")
    reefer_slot = slot_at(world, 12, "DOCK-JAI-D5")
    sid = create(world, reefer_slot, required_dock_type="REEFER", temperature_control_required=True)
    page = world.client("wh_a").get("/warehouse").text
    # Partner warehouse must not appear in Change warehouse dropdown.
    assert "FAC-PARTNER-01" not in page.split(f"/v1/warehouse/shipments/{sid}/warehouse")[1].split("</form>")[0]
    # Allocate slot form warns before replacing an existing booked slot and only offers D5 (Reefer), not D1 (Standard).
    alloc_form = page.split(f"/v1/warehouse/shipments/{sid}/slot")[1].split("</form>")[0]
    assert "Replace the current booked slot" in alloc_form
    assert "D5" in alloc_form and "FAC-JAI-01 D1" not in alloc_form


def test_scenario_3_freeing_missed_no_show_slot_via_board_cross_and_showing_no_slot_state(tmp_path: Path) -> None:
    """Warehouse can free a past no-show slot via the board cross; Shipments tab shows 'Confirmed (no slot)'."""
    world, sid, appt = booked_world(tmp_path)
    query(world, "update shipment_assignments set confirmed_at=? where shipment_id=?", NOW, sid)
    # Advance past the 09:00 slot start; truck never arrived.
    world.clock.advance(timedelta(hours=23, minutes=15))
    client = world.client("wh_a")
    r = client.post(f"/v1/warehouse/appointments/{appt}/free", json={})
    assert r.status_code == 200, r.text
    assert query(world, "select appointment_status, is_current from appointments where appointment_id=?", appt)[0][0] == "CANCELLED"
    page = client.get("/warehouse").text
    assert "Confirmed (no slot)" in page and "No slot allocated" in page


def test_scenario_4_driver_chat_cancel_rejects_pending_facility_change(world: World) -> None:
    """When a driver cancels their confirmed appointment in chat, any pending facility change is closed."""
    open_av(world)
    sid = create(world, slot_at(world, 12))
    query(world, "update shipment_assignments set confirmed_at=? where shipment_id=?", NOW, sid)
    conn = open_connection(path=world.path)
    try:
        asked = facility_change.request_change(conn, world.clock, driver_id="DRV001", shipment_id=sid, to_facility_id="FAC-GGN-01")
        assert asked.status == "REQUESTED"
        res = cancel_confirmed_appointment(conn, "DRV001", "THR-T-FAC-JAI-01", "MSG-C1", sid, world.clock)
        assert res.status == "CANCELLED"
    finally:
        conn.close()
    row = query(world, "select status, reason from facility_change_requests where shipment_id=?", sid)[0]
    assert tuple(row) == ("REJECTED", "APPOINTMENT_CANCELLED")


def test_scenario_5_reopen_button_warns_after_slot_changing_resolution(tmp_path: Path) -> None:
    """Reopening a resolved CANCELLED_BY_OPERATIONS or ASSIGNED_SLOT case warns that the slot change is already saved."""
    world, sid, _appt = booked_world(tmp_path)
    case_id = "ESC-T-FAC-JAI-01"
    query(world, "update escalation_cases set shipment_id=? where case_id=?", sid, case_id)
    client = world.client("wh_a")
    assert client.post(f"/v1/coordinator/cases/{case_id}/claim", json={}).status_code == 200
    assert client.post(f"/v1/coordinator/cases/{case_id}/resolve",
                       json={"resolution_code": "CANCELLED_BY_OPERATIONS", "note": "cancelled"}).status_code == 200
    page = client.get("/warehouse?facility=FAC-JAI-01").text
    assert "already saved and will not be undone" in page
