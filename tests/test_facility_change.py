"""A driver asks to move a load; only the destination warehouse (or an admin) decides; only the destination field changes."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

import pytest

from setuhaul import facility_change as fc
from setuhaul.auth import users
from setuhaul.clock import FakeClock

JAI, GGN = "FAC-JAI-01", "FAC-GGN-01"
SHIP, DRIVER = "SHP1006", "DRV006"


@pytest.fixture()
def world(db: sqlite3.Connection, clock: FakeClock) -> dict[str, Any]:
    admin = users.bootstrap_admin(db, "boss@x.com", clock)
    ggn = users.invite(db, actor=admin, email="g@x.com", role="WAREHOUSE_AUTHORITY", display_name="G", facilities=(GGN,), clock=clock)
    jai = users.invite(db, actor=admin, email="j@x.com", role="WAREHOUSE_AUTHORITY", display_name="J", facilities=(JAI,), clock=clock)
    return {"db": db, "clock": clock, "admin": admin, "ggn": ggn, "jai": jai}


def _ask(w: dict[str, Any], to: str = GGN, driver: str = DRIVER, ship: str = SHIP) -> fc.ChangeResult:
    return fc.request_change(w["db"], w["clock"], driver_id=driver, shipment_id=ship, to_facility_id=to)


def _ship(w: dict[str, Any]) -> dict[str, Any]:
    return dict(w["db"].execute("select * from shipments where shipment_id=?", (SHIP,)).fetchone())


def _decide(w: dict[str, Any], who: str, request_id: str, approve: bool = True) -> str:
    return fc.decide(w["db"], w["clock"], actor=w[who], request_id=request_id, approve=approve).status


def test_one_open_request_per_shipment(world: dict[str, Any]) -> None:
    first = _ask(world)
    assert first.status == "REQUESTED" and first.request_id
    again = _ask(world)
    assert again.status == "ALREADY_PENDING" and again.request_id == first.request_id


def test_foreign_shipment_unknown_and_same_facility_are_refused(world: dict[str, Any]) -> None:
    assert _ask(world, driver="DRV007").status == "NOT_MOVABLE"
    assert _ask(world, to="NOPE").status == "UNKNOWN_FACILITY"
    assert _ask(world, to=JAI).status == "SAME_FACILITY"


@pytest.mark.parametrize("status", ["IN_DOCK", "COMPLETED", "CANCELLED"])
def test_in_dock_or_finished_loads_cannot_move(world: dict[str, Any], status: str) -> None:
    world["db"].execute("update shipments set current_status=? where shipment_id=?", (status, SHIP))
    world["db"].commit()
    assert _ask(world).status == "NOT_MOVABLE"


def test_load_reaching_the_dock_before_approval_is_refused(world: dict[str, Any]) -> None:
    rid = _ask(world).request_id
    world["db"].execute("update shipments set current_status='IN_DOCK' where shipment_id=?", (SHIP,))
    world["db"].commit()
    assert _decide(world, "ggn", rid) == "NOT_MOVABLE" and _ship(world)["destination_facility_id"] == JAI


def test_only_the_destination_warehouse_or_an_admin_decides(world: dict[str, Any]) -> None:
    rid = _ask(world).request_id
    assert _decide(world, "jai", rid) == "NOT_FOUND" and _ship(world)["destination_facility_id"] == JAI
    assert _decide(world, "admin", rid) == "APPROVED"


def test_approval_changes_only_the_destination(world: dict[str, Any]) -> None:
    before = _ship(world)
    assert _decide(world, "ggn", _ask(world).request_id) == "APPROVED"
    after = _ship(world)
    assert after["destination_facility_id"] == GGN
    assert {k: v for k, v in after.items() if k != "destination_facility_id"} == {k: v for k, v in before.items() if k != "destination_facility_id"}
    row = world["db"].execute("select from_facility_id, to_facility_id, status from facility_change_requests").fetchone()
    assert tuple(row) == (JAI, GGN, "APPROVED")


def test_rejection_changes_nothing_and_cannot_be_decided_again(world: dict[str, Any]) -> None:
    before = _ship(world)
    rid = _ask(world).request_id
    assert _decide(world, "ggn", rid, approve=False) == "REJECTED" and _ship(world) == before
    assert _decide(world, "ggn", rid) == "NOT_PENDING" and _ship(world) == before


def test_an_expired_request_cannot_be_approved(world: dict[str, Any]) -> None:
    rid = _ask(world).request_id
    world["clock"].advance(timedelta(minutes=121))
    assert _decide(world, "ggn", rid) == "NOT_PENDING" and _ship(world)["destination_facility_id"] == JAI
    # The lapsed request no longer blocks a new one.
    assert _ask(world).status == "REQUESTED"


def test_requests_and_decisions_are_audited(world: dict[str, Any]) -> None:
    _decide(world, "ggn", _ask(world).request_id)
    actions = [r[0] for r in world["db"].execute("select action from audit_log where action like 'FACILITY_CHANGE%' order by audit_id")]
    assert actions == ["FACILITY_CHANGE_REQUESTED", "FACILITY_CHANGE_APPROVED"]


@pytest.mark.parametrize("status", ["AT_GATE", "WAITING"])
def test_trucks_at_gate_or_waiting_cannot_change_facility(world: dict[str, Any], status: str) -> None:
    req = _ask(world)
    assert req.status == "REQUESTED"
    world["db"].execute("update shipments set current_status=? where shipment_id='SHP1006'", (status,))
    world["db"].commit()
    assert _decide(world, "ggn", req.request_id) == "NOT_MOVABLE"
    assert _ask(world).status == "NOT_MOVABLE"


def test_second_move_preserves_history_of_first_approved_move(world: dict[str, Any]) -> None:
    first = _ask(world, GGN)
    assert _decide(world, "ggn", first.request_id) == "APPROVED"
    world["db"].execute("update facility_change_requests set follow_up='WAITING' where request_id=?", (first.request_id,))
    world["db"].commit()
    second = _ask(world, JAI)
    assert _decide(world, "admin", second.request_id) == "APPROVED"
    row1 = world["db"].execute("select status, reason, follow_up from facility_change_requests where request_id=?", (first.request_id,)).fetchone()
    row2 = world["db"].execute("select from_facility_id, to_facility_id, status from facility_change_requests where request_id=?", (second.request_id,)).fetchone()
    assert tuple(row1) == ("APPROVED", "SUPERSEDED", None)
    assert tuple(row2) == (GGN, JAI, "APPROVED")
