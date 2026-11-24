"""The shared dock board builder and the warehouse board API."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from pathlib import Path

import pytest

from setuhaul.board import build_board
from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from setuhaul.demo_ui import build_demo_app
from tests.auth_helpers import GURUGRAM, JAIPUR, World, build_world, demo_client


@pytest.fixture()
def world(tmp_path: Path, clock: FakeClock) -> World:
    return build_world(tmp_path, clock)


def test_build_board_returns_none_for_an_unknown_facility(world: World) -> None:
    conn = open_connection(path=world.path, readonly=True)
    try:
        assert build_board(conn, "FAC-NOPE", world.clock.now()) is None
    finally:
        conn.close()


def test_build_board_has_the_demo_payload_shape(world: World) -> None:
    conn = open_connection(path=world.path, readonly=True)
    try:
        board = build_board(conn, JAIPUR, world.clock.now())
    finally:
        conn.close()
    assert board is not None
    # Exactly the demo payload minus the console's own clock.
    assert set(board) == {"facility", "hours", "docks", "pending_partner"}
    assert board["facility"] == JAIPUR and board["docks"] and board["hours"]


def test_the_api_board_equals_the_demo_board_without_the_clock(tmp_path: Path) -> None:
    app, _ = build_demo_app(tmp_path / "d")
    client = demo_client(app)
    demo_board = client.get("/demo/api/board", params={"facility_id": "FAC-JAI-01"}).json()
    api_board = client.get("/v1/warehouse/board", params={"facility_id": "FAC-JAI-01"})
    assert api_board.status_code == 200
    body = api_board.json()
    body.pop("request_id", None)
    demo_board.pop("clock")
    assert body == demo_board


def test_board_roles(world: World) -> None:
    assert world.client().get("/v1/warehouse/board", params={"facility_id": JAIPUR}).status_code == 401
    assert world.client("wh_none").get("/v1/warehouse/board", params={"facility_id": JAIPUR}).status_code == 403
    assert world.client("wh_a").get("/v1/warehouse/board", params={"facility_id": JAIPUR}).status_code == 200
    assert world.client("admin").get("/v1/warehouse/board", params={"facility_id": GURUGRAM}).status_code == 200


def test_board_is_scoped_to_the_users_facilities(world: World) -> None:
    a = world.client("wh_a")
    # Another facility and an unknown one look the same: not found.
    other = a.get("/v1/warehouse/board", params={"facility_id": GURUGRAM})
    unknown = a.get("/v1/warehouse/board", params={"facility_id": "FAC-NOPE"})
    assert other.status_code == 404 and unknown.status_code == 404
    assert other.json()["reason_code"] == unknown.json()["reason_code"] == "FACILITY_NOT_FOUND"
    # An admin asking for an unknown facility also gets 404.
    assert world.client("admin").get("/v1/warehouse/board", params={"facility_id": "FAC-NOPE"}).status_code == 404


def test_board_requires_a_facility_id(world: World) -> None:
    assert world.client("wh_a").get("/v1/warehouse/board").status_code == 422
