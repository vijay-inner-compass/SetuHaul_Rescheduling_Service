"""The warehouse sees pending requests and decides them; others get nothing; the driver is told either way."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from setuhaul.auth import users
from setuhaul.db import open_connection
from tests.auth_helpers import sign_in
from tests.test_auto_hold import Chat
from tests.test_demo_driver import _client


@pytest.fixture()
def world(tmp_path: Path) -> dict[str, Any]:
    chat = Chat(tmp_path)
    conn = open_connection(path=chat.deps.db_path)
    admin = users.bootstrap_admin(conn, "boss@x.com", chat.deps.clock)
    clients = {}
    for key, facility in (("ggn", "FAC-GGN-01"), ("jai", "FAC-JAI-01")):
        user = users.invite(conn, actor=admin, email=f"{key}@x.com", role="WAREHOUSE_AUTHORITY", display_name=key,
                            facilities=(facility,), clock=chat.deps.clock)
        clients[key] = _client(chat.app)
        sign_in(clients[key], conn, user, chat.deps.clock)
    conn.close()
    chat.say("please change facility to Gurugram")
    rid = chat.rows("select request_id from facility_change_requests")[0][0]
    return {"chat": chat, "rid": rid, **clients}


def _list(world: dict[str, Any], who: str) -> list[dict[str, Any]]:
    return world[who].get("/v1/warehouse/facility-changes").json()["requests"]  # type: ignore[no-any-return]


def _post(world: dict[str, Any], who: str, verb: str) -> Any:
    return world[who].post(f"/v1/warehouse/facility-changes/{world['rid']}/{verb}", json={})


def test_only_the_destination_warehouse_sees_and_decides(world: dict[str, Any]) -> None:
    assert [r["request_id"] for r in _list(world, "ggn")] == [world["rid"]] and _list(world, "jai") == []
    assert _post(world, "jai", "approve").status_code == 404
    assert _client(world["chat"].app).get("/v1/warehouse/facility-changes").status_code == 401


def test_approve_moves_the_load_and_holds_a_slot(world: dict[str, Any]) -> None:
    r = _post(world, "ggn", "approve")
    assert r.status_code == 200 and r.json()["outcome"] == "HELD" and len(world["chat"].held()) == 1
    assert _post(world, "ggn", "approve").status_code == 409


def test_rejection_tells_the_driver_and_keeps_the_booking(world: dict[str, Any]) -> None:
    chat = world["chat"]
    original = chat.current()
    assert _post(world, "ggn", "reject").status_code == 200
    assert chat.current() == original and chat.held() == []
    assert "not approved" in chat.rows("select message_text from chat_messages order by rowid desc limit 1")[0][0]


def test_page_lists_pending_requests_and_counts_them(world: dict[str, Any]) -> None:
    page = world["ggn"].get("/warehouse").text
    assert "Facility change requests" in page and world["rid"] in page
    assert f"/v1/warehouse/facility-changes/{world['rid']}/approve" in page
    assert "<b>1</b><span>Facility requests</span>" in page
    assert world["rid"] not in world["jai"].get("/warehouse").text


def test_driver_card_shows_the_pending_request(world: dict[str, Any]) -> None:
    booking = world["chat"].client.get("/demo/driver/api/state").json()["booking"]
    assert "Gurugram" in booking["pending_change"]
