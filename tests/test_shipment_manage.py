"""Listing assigned shipments, and changing, cancelling or unlocking one the driver has not confirmed."""

from __future__ import annotations

from pathlib import Path

import pytest

from setuhaul import order_check
from setuhaul.auth import shipments
from setuhaul.auth.projects import allocate
from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from setuhaul.db import parse_operational_timestamp as ts
from setuhaul.escalation import InMemoryEscalationSink
from setuhaul.identity import AuthError
from tests.auth_helpers import World, build_world
from tests.shipment_helpers import DAY, NOW, count, create, open_av, query, slot_at

NUMBER = "ORD-261127-001"


@pytest.fixture()
def world(tmp_path: Path) -> World:
    return build_world(tmp_path, FakeClock(ts(NOW)))


@pytest.fixture()
def made(world: World) -> str:
    open_av(world)
    return create(world, slot_at(world, 12))


def _run(world: World, name: str, shipment_id: str, who: str = "wh_a", **kwargs: object) -> None:
    conn = open_connection(path=world.path)
    try:
        getattr(shipments, name)(conn, actor=world.users[who], shipment_id=shipment_id, clock=world.clock, **kwargs)
    finally:
        conn.close()


def _listed(world: World, who: str = "wh_a") -> list[dict[str, object]]:
    conn = open_connection(path=world.path)
    try:
        return shipments.list_shipments(conn, world.users[who])
    finally:
        conn.close()


def _send(world: World, driver_id: str, text: str = NUMBER) -> str | None:
    conn = open_connection(path=world.path)
    try:
        got = order_check.check(conn, world.clock, driver_id=driver_id, thread_id="THR-T-JAI", text=text, lang="en",
                                sink=InMemoryEscalationSink())
        return None if got is None else got.kind
    finally:
        conn.close()


def _second_driver(world: World) -> None:
    # DRV002 joins the Jaipur user's project, so that user may assign to them.
    conn = open_connection(path=world.path)
    try:
        project = conn.execute("select project_id from project_drivers where driver_id='DRV001'").fetchone()[0]
        allocate(conn, actor=world.users["wh_a"], project_id=project, driver_id="DRV002", clock=world.clock)
    finally:
        conn.close()


def test_the_list_holds_only_app_shipments_in_scope(world: World, made: str) -> None:
    rows = _listed(world)
    assert [r["shipment_id"] for r in rows] == [made]
    assert rows[0] == {"shipment_id": made, "order_reference": NUMBER, "driver_id": "DRV001",
                       "driver_name": rows[0]["driver_name"], "facility_id": "FAC-JAI-01",
                       "planned_start": f"{DAY}T12:00:00+05:30", "state": "WAITING",
                       "pass_state": "ISSUED", "delivered": False, "visit_state": "EXPECTED"}
    assert _listed(world, "wh_b") == [] and len(_listed(world, "admin")) == 1


def test_states(world: World, made: str) -> None:
    query(world, "update shipment_assignments set locked_at=?", NOW)
    assert _listed(world)[0]["state"] == "LOCKED"
    query(world, "update shipment_assignments set locked_at=null, confirmed_at=?", NOW)
    assert _listed(world)[0]["state"] == "CONFIRMED"


def test_another_facility_cannot_touch_it(world: World, made: str) -> None:
    for name, extra in (("cancel_shipment", {}), ("unlock", {}), ("change_driver", {"driver_id": "DRV006", "vehicle_id": "VEH006"})):
        with pytest.raises(AuthError, match="SHIPMENT_NOT_FOUND"):
            _run(world, name, made, who="wh_b", **extra)


def test_a_supplied_shipment_cannot_be_touched(world: World) -> None:
    with pytest.raises(AuthError, match="SHIPMENT_NOT_FOUND"):
        _run(world, "cancel_shipment", "SHP1001", who="admin")


def test_nothing_changes_after_confirmation(world: World, made: str) -> None:
    assert _send(world, "DRV001") == "CONFIRMED"
    for name, extra in (("cancel_shipment", {}), ("change_driver", {"driver_id": "DRV001", "vehicle_id": "VEH001"})):
        with pytest.raises(AuthError, match="ALREADY_CONFIRMED"):
            _run(world, name, made, **extra)


def test_a_new_driver_takes_over(world: World, made: str) -> None:
    _second_driver(world)
    query(world, "update shipment_assignments set failed_attempts=5, locked_at=?", NOW)
    carrier = query(world, "select carrier_id from drivers where driver_id='DRV002'")[0][0]
    vehicle = query(world, "select vehicle_id from vehicles where carrier_id=? and active_flag=1 order by 1", carrier)[0][0]
    _run(world, "change_driver", made, driver_id="DRV002", vehicle_id=vehicle)
    row = query(world, "select * from shipment_assignments where shipment_id=?", made)[0]
    assert row["failed_attempts"] == 0 and row["locked_at"] is None
    assert query(world, "select carrier_id from shipments where shipment_id=?", made)[0][0] == carrier
    assert _send(world, "DRV001") is None
    assert _send(world, "DRV002") == "CONFIRMED"


def test_cancel_frees_the_slot_and_the_number_confirms_nothing(world: World, made: str) -> None:
    slot = slot_at(world, 12)
    _run(world, "cancel_shipment", made)
    assert query(world, "select current_status from shipments where shipment_id=?", made)[0][0] == "CANCELLED"
    appt = query(world, "select appointment_status, is_current from appointments where shipment_id=?", made)[0]
    assert tuple(appt) == ("CANCELLED", 0)
    assert count(world, "appointment_events", f"shipment_id='{made}'") == 1
    assert _send(world, "DRV001") is None and _listed(world)[0]["state"] == "CANCELLED"
    assert create(world, slot, order_number="ORD-261127-002")
    with pytest.raises(AuthError, match="SHIPMENT_NOT_FOUND"):
        _run(world, "cancel_shipment", made)


def test_unlock_resets_the_count(world: World, made: str) -> None:
    with pytest.raises(AuthError, match="NOT_LOCKED"):
        _run(world, "unlock", made)
    for _ in range(5):
        _send(world, "DRV001", "ORD-999999-999")
    _run(world, "unlock", made)
    row = query(world, "select failed_attempts, locked_at from shipment_assignments where shipment_id=?", made)[0]
    assert tuple(row) == (0, None)
    assert _send(world, "DRV001") == "CONFIRMED"
    assert count(world, "audit_log", "action='SHIPMENT_UNLOCKED'") == 1
