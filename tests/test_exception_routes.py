"""The warehouse routes and screen parts for dock blocking, freeing a slot and deleting a driver."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.auth_helpers import JAIPUR, World
from tests.exception_helpers import D1, D2, booked_world
from tests.shipment_helpers import DAY, query, slot_at

PAGE = f"/warehouse?facility={JAIPUR}"


def body(**over: Any) -> dict[str, Any]:
    return {"facility_id": JAIPUR, "dock_id": D1, "event_type": "BREAKDOWN", "reason": "Leveller jammed",
            "starts": f"{DAY}T09:00", "ends": f"{DAY}T10:00"} | over


def expected_of(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"appointment_id": i["appointment_id"], "new_slot_id": i["new_slot_id"]} for i in items if i["action"] != "SKIP"]


def board_cell(world: World, dock: str, hour: int) -> dict[str, Any]:
    board = world.client("wh_a").get(f"/v1/warehouse/board?facility_id={JAIPUR}").json()
    row = next(d for d in board["docks"] if d["code"] == dock)
    return dict(next(s for s in row["slots"] if s["hour"] == hour and s["start_ts"].startswith(DAY)))


def test_preview_then_block_over_http(tmp_path: Path) -> None:
    world, _, _ = booked_world(tmp_path)
    client = world.client("wh_a")
    seen = client.post("/v1/warehouse/dock-blocks/preview", json=body())
    assert seen.status_code == 200 and len(seen.json()["items"]) == 1
    done = client.post("/v1/warehouse/dock-blocks", json=body(expected=expected_of(seen.json()["items"])))
    assert done.status_code == 201 and done.json()["unreachable"] == ["ORD-261127-001"]
    cell = board_cell(world, "D1", 9)
    assert (cell["status"], cell["label"]) == ("BLOCKED", "Leveller jammed")
    reopened = client.post(f"/v1/warehouse/dock-blocks/{done.json()['dock_event_id']}/reopen", json={})
    assert reopened.status_code == 200 and board_cell(world, "D1", 9)["status"] == "AVAILABLE"


def test_stale_expected_answers_409_with_the_new_list(tmp_path: Path) -> None:
    world, _, appt = booked_world(tmp_path)
    r = world.client("wh_a").post("/v1/warehouse/dock-blocks", json=body(expected=[]))
    assert r.status_code == 409 and r.json()["reason_code"] == "PLAN_CHANGED" and r.json()["message"]
    assert [i["appointment_id"] for i in r.json()["items"]] == [appt]


def test_other_warehouse_gets_404(tmp_path: Path) -> None:
    world, _, _ = booked_world(tmp_path)
    r = world.client("wh_b").post("/v1/warehouse/dock-blocks/preview", json=body())
    assert r.status_code == 404 and r.json()["reason_code"] == "FACILITY_NOT_FOUND"


def test_the_page_has_the_block_form_and_reopen_button(tmp_path: Path) -> None:
    world, _, _ = booked_world(tmp_path)
    client = world.client("wh_a")
    page = client.get(PAGE).text
    assert 'id="block-form"' in page and "/app/assets/exceptions.js" in page
    assert client.get("/app/assets/exceptions.js").status_code == 200
    event_id = client.post("/v1/warehouse/dock-blocks", json=body(starts=f"{DAY}T11:00", ends=f"{DAY}T12:00", expected=[]))
    assert f"/v1/warehouse/dock-blocks/{event_id.json()['dock_event_id']}/reopen" in client.get(PAGE).text


def test_blocked_slots_are_not_offered_for_new_shipments(tmp_path: Path) -> None:
    world, _, _ = booked_world(tmp_path)
    client = world.client("wh_a")
    taken, kept = slot_at(world, 10, D2), slot_at(world, 11, D2)
    assert f'value="{taken}"' in client.get(PAGE).text
    client.post("/v1/warehouse/dock-blocks", json=body(dock_id=D2, starts=f"{DAY}T10:00", ends=f"{DAY}T11:00", expected=[]))
    page = client.get(PAGE).text
    assert f'value="{taken}"' not in page and f'value="{kept}"' in page
    assert query(world, "select count(*) from dock_blocks")[0][0] == 1


# ---- the cross on the dock board ------------------------------------------------------------------------
def free_url(appointment_id: str) -> str:
    return f"/v1/warehouse/appointments/{appointment_id}/free"


def test_a_booked_future_cell_has_a_cross_with_a_label(tmp_path: Path) -> None:
    world, _, appt = booked_world(tmp_path)
    world.clock.advance(timedelta(hours=22))  # 08:00 on the day of the booking, so the board shows it
    page = world.client("wh_a").get(PAGE).text
    assert f'data-api="{free_url(appt)}"' in page
    assert 'aria-label="Free dock D1 09:00, order ORD-261127-001"' in page
    cell = board_cell(world, "D1", 9)
    assert (cell["appointment_id"], cell["freeable"]) == (appt, True)


@pytest.mark.parametrize("change", ["held", "started", "on_site", "unloading"])
def test_no_cross_on_held_started_on_site_or_unloading_cells(tmp_path: Path, change: str) -> None:
    world, shipment_id, appt = booked_world(tmp_path)
    world.clock.advance(timedelta(hours=22))
    if change == "held":
        query(world, "update appointments set appointment_status='HELD', is_current=0 where appointment_id=?", appt)
        query(world, "insert into appointment_hold_meta(appointment_id, driver_id, thread_id, option_set_id, request_fingerprint,"
                     " expires_at, expires_epoch) values (?,'DRV001','THR-T-FAC-JAI-01','OS-1','fp','2026-11-28T12:00:00+05:30',?)",
              appt, int(world.clock.now().timestamp()) + 600)
    elif change == "started":
        world.clock.advance(timedelta(hours=1, minutes=5))
    elif change == "on_site":
        query(world, "update shipments set current_status='AT_GATE' where shipment_id=?", shipment_id)
    else:
        query(world, "update appointments set appointment_status='IN_PROGRESS' where appointment_id=?", appt)
    assert free_url(appt) not in world.client("wh_a").get(PAGE).text
    assert board_cell(world, "D1", 9)["freeable"] is False


def test_free_route_frees_and_then_answers_409(tmp_path: Path) -> None:
    world, _, appt = booked_world(tmp_path)
    client = world.client("wh_a")
    first = client.post(free_url(appt), json={})
    assert first.status_code == 200 and first.json()["reached"] is False
    again = client.post(free_url(appt), json={})
    assert again.status_code == 409 and again.json()["reason_code"] == "NOT_BOOKED" and again.json()["message"]
    assert query(world, "select count(*) from notification_outbox where recipient_type='DRIVER'")[0][0] == 1
