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
