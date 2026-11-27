"""Deleting a driver: their unfinished loads are cancelled, nothing is erased, and an admin can bring them back."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from setuhaul.api import resolve_eligible_driver
from setuhaul.auth import driver_removal, onboarding
from setuhaul.auth.warehouse_routes import drivers_in_scope
from setuhaul.db import open_connection
from setuhaul.identity import AuthError
from tests.auth_helpers import World
from tests.exception_helpers import appointment_of, booked_world, run
from tests.shipment_helpers import create, query, slot_at

DRIVER = "DRV001"


def name_of(world: World, driver_id: str = DRIVER) -> str:
    return str(query(world, "select driver_name from drivers where driver_id=?", driver_id)[0][0])


def clear_supplied(world: World) -> None:
    # The sample data gives DRV001 loads of its own; finish them so each test controls what is open.
    query(world, "update shipments set current_status='COMPLETED' where driver_id=? and shipment_id not in"
                 " (select shipment_id from shipment_assignments)", DRIVER)


def make(tmp_path: Path) -> tuple[World, str, str]:
    world, shipment_id, appt = booked_world(tmp_path)
    clear_supplied(world)
    return world, shipment_id, appt


def preview(world: World, who: str = "admin", driver_id: str = DRIVER) -> driver_removal.Preview:
    conn = open_connection(path=world.path)
    try:
        return driver_removal.preview(conn, actor=world.users[who], driver_id=driver_id)
    finally:
        conn.close()


def delete(world: World, who: str = "admin", **over: Any) -> None:
    values: dict[str, Any] = {"driver_id": DRIVER, "confirm_name": name_of(world), "expected": None} | over
    if values["expected"] is None:
        values["expected"] = [s.shipment_id for s in preview(world, who, values["driver_id"]).shipments]
    run(world, driver_removal.delete, actor=world.users[who], **values)


def code_of(fn: Any, *args: Any, **kwargs: Any) -> str:
    with pytest.raises(AuthError) as caught:
        fn(*args, **kwargs)
    return caught.value.code


def visible(world: World) -> bool:
    conn = open_connection(path=world.path)
    try:
        return bool(drivers_in_scope(conn, world.users["admin"], world.clock.now().date(), DRIVER))
    finally:
        conn.close()


def eligible(world: World) -> bool:
    conn = open_connection(path=world.path)
    try:
        return onboarding.driver_eligible(conn, DRIVER, world.clock.now().date())
    finally:
        conn.close()


def test_delete_cancels_shipments_frees_slots_and_hides_the_driver(tmp_path: Path) -> None:
    world, shipment_id, appt = make(tmp_path)
    [doomed] = preview(world).shipments
    assert (doomed.shipment_id, doomed.dock_code, doomed.blocked_by) == (shipment_id, "D1", None)
    delete(world)
    assert query(world, "select current_status from shipments where shipment_id=?", shipment_id)[0][0] == "CANCELLED"
    assert tuple(query(world, "select appointment_status, cancellation_reason from appointments where appointment_id=?", appt)[0]) == (
        "CANCELLED", "DRIVER_DELETED")
    row = query(world, "select driver_status, phone from drivers where driver_id=?", DRIVER)[0]
    assert row["driver_status"] == "INACTIVE" and not visible(world) and not eligible(world)
    assert not query(world, "select 1 from project_drivers where driver_id=? and released_at is null", DRIVER)
    conn = open_connection(path=world.path)
    try:
        with pytest.raises(AuthError):
            resolve_eligible_driver(conn, row["phone"], world.clock)
    finally:
        conn.close()


def test_a_new_shipment_after_the_warning_changes_nothing(tmp_path: Path) -> None:
    world, shipment_id, appt = make(tmp_path)
    shown = [s.shipment_id for s in preview(world).shipments]
    create(world, slot_at(world, 10), order_number="ORD-261127-002")
    assert code_of(delete, world, expected=shown) == "PLAN_CHANGED"
    assert appointment_of(world, shipment_id) == appt and eligible(world)


def test_wrong_name_and_on_site_are_refused(tmp_path: Path) -> None:
    world, shipment_id, appt = make(tmp_path)
    assert code_of(delete, world, confirm_name="Someone Else") == "NAME_MISMATCH"
    query(world, "update shipments set current_status='AT_GATE' where shipment_id=?", shipment_id)
    assert preview(world).shipments[0].blocked_by == "TRUCK_ON_SITE"
    assert code_of(delete, world) == "DRIVER_ON_SITE"
    assert appointment_of(world, shipment_id) == appt


def test_warehouse_scope_rules(tmp_path: Path) -> None:
    world, shipment_id, _ = make(tmp_path)
    # A supplied driver was added by nobody, so only an admin may delete them.
    assert code_of(preview, world, "wh_a") == "DRIVER_NOT_FOUND"
    query(world, "insert into driver_profiles(driver_id, licence_expiry, vehicle_id, approval_status, created_by_user_id)"
                 " values (?,'2030-01-01','VEH001','APPROVED',?)", DRIVER, world.users["wh_a"].user_id)
    assert preview(world, "wh_a").driver_id == DRIVER
    query(world, "update shipments set destination_facility_id='FAC-GGN-01' where shipment_id=?", shipment_id)
    assert code_of(delete, world, "wh_a") == "OTHER_FACILITY_SHIPMENTS"


def test_a_deleted_driver_cannot_be_approved_or_added_again(tmp_path: Path) -> None:
    world, _, _ = make(tmp_path)
    phone = query(world, "select phone from drivers where driver_id=?", DRIVER)[0][0]
    delete(world)
    assert code_of(run, world, onboarding.approve, actor=world.users["admin"], driver_id=DRIVER) == "DRIVER_DELETED"
    assert code_of(run, world, onboarding.add_driver, actor=world.users["admin"], name="Again", phone=phone,
                   licence_number="NEW-1", licence_expiry=date(2030, 1, 1), carrier_id="CAR001",
                   vehicle_id="VEH001") == "DRIVER_DELETED_RESTORE"
    assert code_of(preview, world) == "DRIVER_NOT_FOUND"


def test_restore_brings_the_driver_back_but_not_the_shipments(tmp_path: Path) -> None:
    world, shipment_id, _ = make(tmp_path)
    delete(world)
    assert code_of(run, world, driver_removal.restore, actor=world.users["wh_a"], driver_id=DRIVER) == "FORBIDDEN"
    run(world, driver_removal.restore, actor=world.users["admin"], driver_id=DRIVER)
    assert visible(world) and eligible(world)
    assert query(world, "select current_status from shipments where shipment_id=?", shipment_id)[0][0] == "CANCELLED"
    assert code_of(run, world, driver_removal.restore, actor=world.users["admin"], driver_id=DRIVER) == "NOT_DELETED"
