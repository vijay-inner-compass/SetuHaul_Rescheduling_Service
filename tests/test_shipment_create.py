"""Opening availability and creating an assigned shipment from the warehouse side."""

from __future__ import annotations

from pathlib import Path

import pytest

from setuhaul.clock import FakeClock
from setuhaul.db import parse_operational_timestamp as ts
from setuhaul.identity import AuthError
from tests.auth_helpers import World, build_world
from tests.shipment_helpers import D1, DAY, NOW, count, create, open_av, query, slot_at


@pytest.fixture()
def world(tmp_path: Path) -> World:
    return build_world(tmp_path, FakeClock(ts(NOW)))


def _ready(world: World) -> str:
    open_av(world)
    return slot_at(world, 12)


def test_availability_creates_slots_and_labour(world: World) -> None:
    r = open_av(world, last_date="2026-11-29")
    assert r.slots_created == 8 and r.days_unchanged == []
    assert count(world, "labour_windows", "source='WAREHOUSE_UI'") == 2


def test_repeating_availability_adds_nothing(world: World) -> None:
    open_av(world, last_date="2026-11-29")
    again = open_av(world, last_date="2026-11-29")
    assert again.slots_created == 0 and again.days_unchanged == [DAY, "2026-11-29"]
    assert count(world, "labour_windows", "source='WAREHOUSE_UI'") == 2


def test_an_overlapping_slot_is_not_added(world: World) -> None:
    open_av(world, from_time="09:00", to_time="11:00")
    assert open_av(world, from_time="09:30", to_time="11:30").slots_created == 0


@pytest.mark.parametrize(("over", "code"), [
    ({"facility_id": "FAC-GGN-01", "dock_ids": ["DOCK-GGN-D1"]}, "FACILITY_NOT_FOUND"),
    ({"dock_ids": ["DOCK-GGN-D1"]}, "DOCK_NOT_FOUND"),
    ({"dock_ids": []}, "DOCK_NOT_FOUND"),
    ({"last_date": "2026-12-12"}, "INVALID_DATES"),
    ({"first_date": "2026-11-26", "last_date": "2026-11-26"}, "INVALID_DATES"),
    ({"last_date": "2026-11-27"}, "INVALID_DATES"),
    ({"from_time": "13:00", "to_time": "09:00"}, "INVALID_TIMES"),
    ({"from_time": "00:00", "to_time": "04:00"}, "INVALID_TIMES"),
])
def test_availability_refusals(world: World, over: dict[str, object], code: str) -> None:
    with pytest.raises(AuthError, match=code):
        open_av(world, **over)
    assert count(world, "labour_windows") == 0


def test_create_writes_every_row_and_waits_for_the_driver(world: World) -> None:
    sid = create(world, _ready(world))
    ship = query(world, "select * from shipments where shipment_id=?", sid)[0]
    assert ship["current_status"] == "ASSIGNED" and ship["carrier_id"] == "CAR001"
    assert ship["original_eta_ts"] == ship["latest_eta_ts"] == f"{DAY}T12:00:00+05:30"
    assert ship["planned_departure_ts"] == "2026-11-27T20:00:00+05:30"
    appt = query(world, "select * from appointments where shipment_id=?", sid)[0]
    assert (appt["appointment_status"], appt["booking_source"], appt["is_current"]) == ("CONFIRMED", "WAREHOUSE", 1)
    assert query(world, "select source from consignee_commitments where shipment_id=?", sid)[0]["source"] == "WAREHOUSE_UI"
    assert query(world, "select confirmed_at from shipment_assignments where shipment_id=?", sid)[0]["confirmed_at"] is None
    assert count(world, "v_driver_shipments", f"shipment_id='{sid}'") == 0
    assert count(world, "audit_log", "action='SHIPMENT_CREATED'") == 1


def test_an_arrival_deadline_before_the_slot_is_allowed(world: World) -> None:
    assert create(world, _ready(world), latest_arrival=f"{DAY}T08:00:00", penalty_applies=True)


def test_the_same_slot_twice_is_refused_and_leaves_nothing(world: World) -> None:
    slot = _ready(world)
    create(world, slot)
    with pytest.raises(AuthError, match="SLOT_NOT_AVAILABLE"):
        create(world, slot, order_number="ORD-261127-002")
    assert count(world, "shipments", "order_reference='ORD-261127-002'") == 0


def test_the_same_number_written_differently_is_a_duplicate(world: World) -> None:
    create(world, _ready(world))
    with pytest.raises(AuthError, match="DUPLICATE_ORDER_NUMBER"):
        create(world, slot_at(world, 11), order_number="ord 261127 001")


@pytest.mark.parametrize(("over", "code"), [
    ({"order_number": "PO-1"}, "BAD_ORDER_NUMBER"),
    ({"driver_id": "DRV006"}, "DRIVER_NOT_FOUND"),
    ({"vehicle_id": "VEH006"}, "VEHICLE_CARRIER_MISMATCH"),
    ({"facility_id": "FAC-GGN-01"}, "FACILITY_NOT_FOUND"),
    ({"slot_id": "SL-NOPE"}, "SLOT_NOT_AVAILABLE"),
    ({"load_weight_kg": 24000}, "LOAD_DOES_NOT_FIT"),
    ({"expected_unload_min": 90}, "LOAD_DOES_NOT_FIT"),
    ({"planned_departure": f"{DAY}T12:30:00"}, "INVALID_DEPARTURE"),
])
def test_create_refusals_leave_nothing(world: World, over: dict[str, object], code: str) -> None:
    slot = _ready(world)
    before = [count(world, t) for t in ("shipments", "appointments", "consignee_commitments", "shipment_assignments")]
    with pytest.raises(AuthError, match=code):
        create(world, over.pop("slot_id", slot) if "slot_id" in over else slot, **{k: v for k, v in over.items() if k != "slot_id"})
    assert [count(world, t) for t in ("shipments", "appointments", "consignee_commitments", "shipment_assignments")] == before


def test_an_ineligible_driver_is_refused(world: World) -> None:
    slot = _ready(world)
    query(world, "update drivers set driver_status='INACTIVE' where driver_id='DRV001'")
    with pytest.raises(AuthError, match="DRIVER_NOT_ELIGIBLE"):
        create(world, slot)


def test_a_partner_facility_is_refused(world: World) -> None:
    query(world, "update facilities set is_partner=1 where facility_id='FAC-JAI-01'")
    with pytest.raises(AuthError, match="PARTNER_FACILITY"):
        open_av(world, dock_ids=[D1])
