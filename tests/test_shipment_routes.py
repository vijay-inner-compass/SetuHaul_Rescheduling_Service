"""The Shipments routes and the Warehouse tab."""

from __future__ import annotations

from pathlib import Path

import pytest

from setuhaul.clock import FakeClock
from setuhaul.db import parse_operational_timestamp as ts
from tests.auth_helpers import World, build_world
from tests.shipment_helpers import D1, DAY, NOW, create, open_av, query, slot_at

AVAILABILITY = {"facility_id": "FAC-JAI-01", "first_date": DAY, "last_date": DAY, "dock_ids": [D1], "from_time": "09:00",
                "to_time": "13:00", "slot_minutes": "60", "crews": "4", "equipment_available": "true"}


def _form(slot_id: str) -> dict[str, str]:
    # Every value a string, as the page's script sends them; optional empty fields are left out.
    return {"order_number": "ORD-261127-001", "driver_id": "DRV001", "vehicle_id": "VEH001", "origin_name": "Test Plant",
            "origin_city": "Neemrana", "facility_id": "FAC-JAI-01", "customer_name": "Acme <script>", "product_category": "FMCG",
            "load_weight_kg": "12000", "required_dock_type": "STANDARD", "temperature_control_required": "false",
            "priority_code": "NORMAL", "expected_unload_min": "50", "planned_departure": "2026-11-27T20:00", "slot_id": slot_id,
            "penalty_applies": "false"}


@pytest.fixture()
def world(tmp_path: Path) -> World:
    return build_world(tmp_path, FakeClock(ts(NOW)))


def test_roles(world: World) -> None:
    assert world.client().get("/v1/warehouse/shipments").status_code == 401
    assert world.client().post("/v1/warehouse/availability", json=AVAILABILITY).status_code == 401
    assert world.client("wh_none").get("/v1/warehouse/shipments").status_code == 403
    assert world.client("wh_a").get("/v1/warehouse/shipments").json()["shipments"] == []


def test_open_create_and_manage_through_the_routes(world: World) -> None:
    client = world.client("wh_a")
    opened = client.post("/v1/warehouse/availability", json=AVAILABILITY)
    assert opened.status_code == 200 and opened.json()["slots_created"] == 4 and opened.json()["days_unchanged"] == []
    made = client.post("/v1/warehouse/shipments", json=_form(slot_at(world, 12)))
    assert made.status_code == 201
    sid = made.json()["shipment_id"]
    assert [s["state"] for s in client.get("/v1/warehouse/shipments").json()["shipments"]] == ["WAITING"]
    assert client.post("/v1/warehouse/shipments", json=_form(slot_at(world, 11))).status_code == 409
    assert client.post(f"/v1/warehouse/shipments/{sid}/unlock", json={}).json()["reason_code"] == "NOT_LOCKED"
    assert world.client("wh_b").post(f"/v1/warehouse/shipments/{sid}/cancel", json={}).status_code == 404
    moved = client.post(f"/v1/warehouse/shipments/{sid}/driver", json={"driver_id": "DRV001", "vehicle_id": "VEH001"})
    assert moved.status_code == 200
    assert client.post(f"/v1/warehouse/shipments/{sid}/cancel", json={}).status_code == 200


def test_bad_values_and_other_sites_are_refused(world: World) -> None:
    client = world.client("wh_a")
    assert client.post("/v1/warehouse/availability", json=AVAILABILITY | {"extra": "x"}).status_code == 422
    assert client.post("/v1/warehouse/availability", json=AVAILABILITY | {"first_date": "soon"}).status_code == 422
    r = client.post("/v1/warehouse/availability", json=AVAILABILITY, headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["reason_code"] == "CROSS_SITE_REQUEST"


def test_the_page_shows_the_forms_and_every_state(world: World) -> None:
    open_av(world)
    waiting = create(world, slot_at(world, 9), customer_name="Acme <script>")
    locked = create(world, slot_at(world, 10), order_number="ORD-261127-002")
    done = create(world, slot_at(world, 11), order_number="ORD-261127-003")
    query(world, "update shipment_assignments set locked_at=? where shipment_id=?", NOW, locked)
    query(world, "update shipment_assignments set confirmed_at=? where shipment_id=?", NOW, done)
    page = world.client("wh_a").get("/warehouse").text
    assert 'data-tab="shipments"' in page and 'id="availability-form"' in page and 'data-api="/v1/warehouse/shipments"' in page
    for text in ("Waiting for driver", "Locked", "Confirmed"):
        assert text in page
    assert f"/v1/warehouse/shipments/{waiting}/cancel" in page and f"/v1/warehouse/shipments/{locked}/unlock" in page
    assert f"/v1/warehouse/shipments/{done}/cancel" not in page
    assert "<script>Acme" not in page and "Acme <script>" not in page
    # A taken slot is not offered again; a free one is.
    assert f'value="{slot_at(world, 12)}"' in page and f'value="{slot_at(world, 9)}"' not in page


def test_the_other_facilitys_user_sees_none_of_it(world: World) -> None:
    open_av(world)
    create(world, slot_at(world, 9))
    page = world.client("wh_b").get("/warehouse").text
    assert "ORD-261127-001" not in page and f'value="{slot_at(world, 12)}"' not in page


def test_a_refusal_says_why_in_plain_words(world: World) -> None:
    client = world.client("wh_a")
    client.post("/v1/warehouse/availability", json=AVAILABILITY)
    heavy = client.post("/v1/warehouse/shipments", json=_form(slot_at(world, 12)) | {"load_weight_kg": "24000"}).json()
    assert heavy["reason_code"] == "LOAD_DOES_NOT_FIT" and "too heavy for the dock" in heavy["message"]
    late = client.post("/v1/warehouse/availability", json=AVAILABILITY | {"from_time": "00:00", "to_time": "04:00"}).json()
    assert late["reason_code"] == "INVALID_TIMES" and "opening hours" in late["message"]


def test_every_facility_keeps_its_own_slots_in_the_picker(world: World) -> None:
    # Far more than 500 earlier Jaipur slots must not push the Gurugram ones off the list.
    docks = [f"DOCK-JAI-D{n}" for n in range(1, 7)]
    open_av(world, last_date="2026-12-11", dock_ids=docks, from_time="06:00", to_time="22:00")
    open_av(world, who="wh_b", facility_id="FAC-GGN-01", dock_ids=["DOCK-GGN-D1"], first_date="2026-12-11", last_date="2026-12-11")
    page = world.client("admin").get("/warehouse").text
    late = query(world, "select slot_id from appointment_slots where dock_id='DOCK-GGN-D1' and slot_start_ts like '2026-12-11%'")[0][0]
    assert f'value="{late}"' in page


def test_the_picker_says_when_nothing_is_open(world: World) -> None:
    page = world.client("wh_a").get("/warehouse").text
    assert "No open slots: open availability first" in page and "data-form-result" in page


def test_changing_warehouse_frees_slot_and_allows_allocating_at_new_warehouse(world: World) -> None:
    open_av(world)
    open_av(world, who="wh_b", facility_id="FAC-GGN-01", dock_ids=["DOCK-GGN-D1"])
    old_slot = slot_at(world, 12)
    sid = create(world, old_slot)
    # Even a confirmed shipment (not yet on-site) can have its warehouse changed.
    query(world, "update shipment_assignments set confirmed_at=? where shipment_id=?", NOW, sid)
    r = world.client("wh_a").post(f"/v1/warehouse/shipments/{sid}/warehouse", json={"facility_id": "FAC-GGN-01"})
    assert r.status_code == 200, r.text
    assert query(world, "select destination_facility_id from shipments where shipment_id=?", sid)[0][0] == "FAC-GGN-01"
    old_appt = query(world, "select appointment_status, is_current, cancellation_reason from appointments where slot_id=?", old_slot)[0]
    assert tuple(old_appt) == ("CANCELLED", 0, "WAREHOUSE_CHANGED")
    # Old slot at Jaipur is now free for another shipment.
    assert create(world, old_slot, order_number="ORD-261127-002")
    # Gurugram now sees the shipment as unallocated and can allocate a slot there.
    page = world.client("wh_b").get("/warehouse").text
    assert "ORD-261127-001" in page and "No slot allocated" in page and f"/v1/warehouse/shipments/{sid}/slot" in page
    new_slot = slot_at(world, 12, "DOCK-GGN-D1")
    alloc = world.client("wh_b").post(f"/v1/warehouse/shipments/{sid}/slot", json={"slot_id": new_slot})
    assert alloc.status_code == 200, alloc.text
    live = query(world, "select slot_id, appointment_status, is_current from appointments where shipment_id=? and is_current=1", sid)[0]
    assert tuple(live) == (new_slot, "CONFIRMED", 1)


def test_admin_can_change_warehouse_and_allocate_slot_in_one_step(world: World) -> None:
    open_av(world)
    open_av(world, who="admin", facility_id="FAC-GGN-01", dock_ids=["DOCK-GGN-D1"])
    old_slot = slot_at(world, 12)
    new_slot = slot_at(world, 12, "DOCK-GGN-D1")
    sid = create(world, old_slot)
    r = world.client("admin").post(f"/v1/warehouse/shipments/{sid}/warehouse",
                                   json={"facility_id": "FAC-GGN-01", "slot_id": new_slot})
    assert r.status_code == 200, r.text
    assert tuple(query(world, "select appointment_status, is_current from appointments where slot_id=?", old_slot)[0]) == ("CANCELLED", 0)
    live = query(world, "select slot_id, appointment_status from appointments where shipment_id=? and is_current=1", sid)[0]
    assert tuple(live) == (new_slot, "CONFIRMED")


def test_warehouse_change_refuses_same_facility_or_on_site_truck(world: World) -> None:
    open_av(world)
    sid = create(world, slot_at(world, 12))
    same = world.client("wh_a").post(f"/v1/warehouse/shipments/{sid}/warehouse", json={"facility_id": "FAC-JAI-01"})
    assert same.status_code == 409 and same.json()["reason_code"] == "SAME_FACILITY"
    query(world, "update shipments set current_status='IN_DOCK' where shipment_id=?", sid)
    onsite = world.client("wh_a").post(f"/v1/warehouse/shipments/{sid}/warehouse", json={"facility_id": "FAC-GGN-01"})
    assert onsite.status_code == 409 and onsite.json()["reason_code"] == "ORIGINAL_IN_PROGRESS"


def test_warehouse_change_expires_pending_partner_requests(world: World) -> None:
    open_av(world)
    open_av(world, who="admin", facility_id="FAC-GGN-01", dock_ids=["DOCK-GGN-D1"])
    sid = create(world, slot_at(world, 12))
    # Simulate an open partner request for this shipment at Jaipur.
    contact = query(world, "select contact_id from facility_contacts limit 1")[0][0]
    query(world, "insert into partner_requests(request_ref, shipment_id, facility_id, contact_id, proposed_start, proposed_end,"
                 " status, email_status, created_at, expires_at) values ('PR-TEST-1',?,'FAC-JAI-01',?,"
                 "'2026-11-28T12:00:00+05:30','2026-11-28T13:00:00+05:30','PENDING_PARTNER','SENT',?,?)", sid, contact, NOW, NOW)
    r = world.client("admin").post(f"/v1/warehouse/shipments/{sid}/warehouse", json={"facility_id": "FAC-GGN-01"})
    assert r.status_code == 200, r.text
    assert query(world, "select status from partner_requests where request_ref='PR-TEST-1'")[0][0] == "EXPIRED"
