"""What Operations does for a driver: book the slot a case is resolved with, write to them, and resend a failed message."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from setuhaul.db import open_connection
from setuhaul.gps import store_ping
from setuhaul.schemas import GpsPing
from tests.auth_helpers import World
from tests.exception_helpers import D1, D2, appointment_of, booked_world
from tests.shipment_helpers import DAY, query, slot_at

PAGE = "/warehouse?facility=FAC-JAI-01"
CASE = "ESC-T-FAC-JAI-01"


def case_world(tmp_path: Path) -> tuple[World, str, str, Any]:
    """A booked shipment (D1 09:00) whose driver has a case claimed by wh_a."""
    world, shipment_id, appt = booked_world(tmp_path)
    query(world, "update escalation_cases set shipment_id=? where case_id=?", shipment_id, CASE)
    client = world.client("wh_a")
    assert client.post(f"/v1/coordinator/cases/{CASE}/claim", json={}).status_code == 200
    return world, shipment_id, appt, client


def resolve(client: Any, **body: Any) -> Any:
    return client.post(f"/v1/coordinator/cases/{CASE}/resolve",
                       json={"resolution_code": "ASSIGNED_SLOT", "note": "slot is free"} | body)


def case_status(world: World) -> str:
    return str(query(world, "select status from escalation_cases where case_id=?", CASE)[0][0])


def chat(world: World) -> list[str]:
    # Every thread of the driver: a message written later goes to whichever is newest.
    return [r[0] for r in query(world, "select m.message_text from chat_messages m join chat_threads t using(thread_id)"
                                       " where t.driver_id='DRV001' and m.sender_type='OPERATIONS' order by m.rowid")]


def notices(world: World) -> list[Any]:
    return query(world, "select outbox_id, status, attempts, payload_json from notification_outbox"
                        " where recipient_type='DRIVER' order by outbox_id")


def test_resolving_with_a_slot_moves_the_booking(tmp_path: Path) -> None:
    world, shipment_id, old, client = case_world(tmp_path)
    new_slot = slot_at(world, 11, D2)
    r = resolve(client, slot_id=new_slot)
    assert r.status_code == 200, r.text
    new = appointment_of(world, shipment_id)
    assert new != old
    row = query(world, "select slot_id, appointment_status from appointments where appointment_id=?", new)[0]
    assert (row["slot_id"], row["appointment_status"]) == (new_slot, "CONFIRMED")
    assert query(world, "select appointment_status from appointments where appointment_id=?", old)[0][0] == "CANCELLED"
    assert case_status(world) == "RESOLVED"
    assert "slot is free" in chat(world)[-1] and "11:00" in chat(world)[-1] and "D2" in chat(world)[-1]
    assert any("11:00" in n["payload_json"] for n in notices(world))


def test_a_taken_slot_changes_nothing(tmp_path: Path) -> None:
    world, shipment_id, old, client = case_world(tmp_path)
    r = resolve(client, slot_id=slot_at(world, 9, D1))
    assert r.status_code == 409 and r.json()["reason_code"] == "SLOT_NOT_AVAILABLE" and r.json()["message"]
    assert appointment_of(world, shipment_id) == old and case_status(world) == "CLAIMED"


def test_assigned_a_slot_needs_the_slot(tmp_path: Path) -> None:
    world, _shipment_id, _old, client = case_world(tmp_path)
    r = resolve(client)
    assert r.status_code == 422 and r.json()["reason_code"] == "SLOT_REQUIRED"
    assert case_status(world) == "CLAIMED"


def test_a_slot_with_another_outcome_is_refused(tmp_path: Path) -> None:
    world, _, _, client = case_world(tmp_path)
    r = resolve(client, resolution_code="ADVISED_DRIVER", slot_id=slot_at(world, 11, D2))
    assert r.status_code == 422 and case_status(world) == "CLAIMED"


def test_claim_and_resolve_are_queued_for_the_phone(tmp_path: Path) -> None:
    world, _, _, client = case_world(tmp_path)
    assert len(notices(world)) == 1
    assert resolve(client, resolution_code="ADVISED_DRIVER", note="call the gate").status_code == 200
    assert len(notices(world)) == 2 and "call the gate" in notices(world)[-1]["payload_json"]


def test_the_case_form_offers_the_free_slots(tmp_path: Path) -> None:
    world, _, _, client = case_world(tmp_path)
    page = client.get(PAGE).text
    card = page.split(f'data-key="case:{CASE}"')[1].split("<!--case-->")[0]
    assert 'name="slot_id"' in card
    assert f'value="{slot_at(world, 11, D2)}"' in card and f'value="{slot_at(world, 9, D1)}"' not in card


def message_url(driver: str = "DRV001") -> str:
    return f"/v1/warehouse/drivers/{driver}/messages"


def test_operations_can_write_after_the_case_is_closed(tmp_path: Path) -> None:
    world, _, _, client = case_world(tmp_path)
    assert resolve(client, resolution_code="ADVISED_DRIVER").status_code == 200
    r = client.post(message_url(), json={"text": "Gate 2 today"})
    assert r.status_code == 200, r.text
    assert r.json()["queued"] is True
    assert chat(world)[-1] == "Gate 2 today" and "Gate 2 today" in notices(world)[-1]["payload_json"]
    page = client.get(PAGE).text
    assert f'data-api="{message_url()}"' in page and "Gate 2 today" in page


def test_another_warehouse_cannot_write_to_the_driver(tmp_path: Path) -> None:
    world, _, _, _ = case_world(tmp_path)
    r = world.client("wh_b").post(message_url(), json={"text": "hi"})
    assert r.status_code == 404 and chat(world)[-1] != "hi"


def test_an_empty_message_is_refused(tmp_path: Path) -> None:
    _, _, _, client = case_world(tmp_path)
    assert client.post(message_url(), json={"text": "   "}).status_code == 422


def test_a_failed_message_can_be_sent_again(tmp_path: Path) -> None:
    world, _, _, client = case_world(tmp_path)
    outbox_id = notices(world)[-1]["outbox_id"]
    url = f"/v1/warehouse/notices/{outbox_id}/resend"
    # Still queued: there is nothing to resend yet.
    assert client.post(url, json={}).status_code == 409
    assert url not in client.get(PAGE).text
    query(world, "update notification_outbox set status='PERMANENT_FAILURE', attempts=5 where outbox_id=?", outbox_id)
    assert url in client.get(PAGE).text
    assert world.client("wh_b").post(url, json={}).status_code == 404
    assert client.post(url, json={}).status_code == 200
    again = notices(world)[-1]
    assert (again["status"], again["attempts"]) == ("PENDING", 0)


@pytest.mark.parametrize("status", ["AT_GATE", "WAITING", "IN_DOCK"])
def test_a_truck_recorded_on_site_keeps_its_booking(tmp_path: Path, status: str) -> None:
    world, shipment_id, old, client = case_world(tmp_path)
    query(world, "update shipments set current_status=? where shipment_id=?", status, shipment_id)
    r = resolve(client, slot_id=slot_at(world, 11, D2))
    assert r.status_code == 409 and r.json()["reason_code"] == "ORIGINAL_IN_PROGRESS"
    assert appointment_of(world, shipment_id) == old and case_status(world) == "CLAIMED"


def test_a_slot_before_the_truck_can_arrive_is_refused(tmp_path: Path) -> None:
    world, shipment_id, old, client = case_world(tmp_path)
    query(world, "update shipments set latest_eta_ts=?, original_eta_ts=? where shipment_id=?",
          f"{DAY}T12:00:00+05:30", f"{DAY}T12:00:00+05:30", shipment_id)
    r = resolve(client, slot_id=slot_at(world, 11, D2))
    assert r.status_code == 422 and "cannot arrive" in r.json()["message"]
    assert appointment_of(world, shipment_id) == old and case_status(world) == "CLAIMED"


def test_a_fresh_position_far_away_rules_out_an_early_slot(tmp_path: Path) -> None:
    world, shipment_id, old, client = case_world(tmp_path)
    # 40 minutes before the slot, with the truck seen near Mumbai.
    world.clock.advance(timedelta(hours=24, minutes=20))
    client = world.client("wh_a")
    conn = open_connection(path=world.path)
    try:
        store_ping(conn, shipment_id, GpsPing(lat=19.07, lon=72.87, recorded_at=world.clock.now()), world.clock, "DRIVER_APP")
        conn.commit()
    finally:
        conn.close()
    r = resolve(client, slot_id=slot_at(world, 11, D2))
    assert r.status_code == 422 and "cannot arrive" in r.json()["message"], r.text
    assert appointment_of(world, shipment_id) == old


def test_resolving_with_cancelled_by_operations_frees_the_dock_slot(tmp_path: Path) -> None:
    world, shipment_id, old, client = case_world(tmp_path)
    old_slot = slot_at(world, 9, D1)
    r = resolve(client, resolution_code="CANCELLED_BY_OPERATIONS", note="driver cannot make it")
    assert r.status_code == 200, r.text
    row = query(world, "select appointment_status, is_current, cancellation_reason from appointments where appointment_id=?", old)[0]
    assert tuple(row) == ("CANCELLED", 0, "CANCELLED_BY_OPERATIONS")
    assert query(world, "select appointment_id from appointments where shipment_id=? and is_current=1", shipment_id) == []
    assert case_status(world) == "RESOLVED"
    assert "driver cannot make it" in chat(world)[-1] and "D1" in chat(world)[-1] and "freed" in chat(world)[-1]
    assert any("freed" in n["payload_json"] for n in notices(world))
    # The freed dock slot is now open on the board.
    board = client.get("/v1/warehouse/board", params={"facility_id": "FAC-JAI-01"}).json()
    d1_slots = [s for d in board["docks"] if d["code"] == "D1" for s in d["slots"] if s["slot_id"] == old_slot]
    assert d1_slots and d1_slots[0]["status"] == "AVAILABLE"


def test_cancelled_by_operations_refuses_when_truck_is_on_site(tmp_path: Path) -> None:
    world, shipment_id, old, client = case_world(tmp_path)
    query(world, "update shipments set current_status='IN_DOCK' where shipment_id=?", shipment_id)
    r = resolve(client, resolution_code="CANCELLED_BY_OPERATIONS", note="cancel")
    assert r.status_code == 409 and r.json()["reason_code"] == "ORIGINAL_IN_PROGRESS"
    assert appointment_of(world, shipment_id) == old and case_status(world) == "CLAIMED"


def test_cancelled_by_operations_refuses_completed_or_partner_shipment(tmp_path: Path) -> None:
    world, shipment_id, old, client = case_world(tmp_path)
    query(world, "update shipments set current_status='COMPLETED' where shipment_id=?", shipment_id)
    r = resolve(client, resolution_code="CANCELLED_BY_OPERATIONS", note="cancel")
    assert r.status_code == 409 and r.json()["reason_code"] == "SHIPMENT_FINISHED"
    query(world, "update shipments set current_status='ASSIGNED' where shipment_id=?", shipment_id)
    query(world, "update facilities set is_partner=1 where facility_id='FAC-JAI-01'")
    r2 = resolve(client, resolution_code="CANCELLED_BY_OPERATIONS", note="cancel")
    assert r2.status_code == 422 and r2.json()["reason_code"] == "PARTNER_FACILITY"
    assert appointment_of(world, shipment_id) == old and case_status(world) == "CLAIMED"


def test_case_without_shipment_says_no_shipment_linked(tmp_path: Path) -> None:
    world, _, _, _ = case_world(tmp_path)
    none_case = world.cases["NONE"]
    admin = world.client("admin")
    assert admin.post(f"/v1/coordinator/cases/{none_case}/claim", json={}).status_code == 200
    page = admin.get(PAGE).text
    card = page.split(f'data-key="case:{none_case}"')[1].split("<!--case-->")[0]
    assert "No shipment linked to this case" in card
    assert "No open slots: open availability first" not in card
