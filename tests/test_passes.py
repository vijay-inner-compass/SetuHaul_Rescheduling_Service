"""Issuing, revoking, re-timing and expiring passes."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from setuhaul import passes
from setuhaul.clock import FakeClock
from setuhaul.db import immediate, open_connection
from setuhaul.db import parse_operational_timestamp as ts
from setuhaul.dock_pass import hash_code, normalise
from setuhaul.identity import AuthError
from tests.auth_helpers import World, build_world
from tests.shipment_helpers import NOW, create, open_av, query, slot_at


@pytest.fixture()
def world(tmp_path: Path) -> World:
    return build_world(tmp_path, FakeClock(ts(NOW)))


@pytest.fixture()
def made(world: World) -> str:
    open_av(world)
    return create(world, slot_at(world, 12))


def issue(world: World, shipment_id: str) -> str:
    conn = open_connection(path=world.path)
    try:
        with immediate(conn):
            return passes.issue(conn, shipment_id, world.clock, world.users["wh_a"])
    finally:
        conn.close()


def run(world: World, name: str, *args: object, **kwargs: object) -> object:
    conn = open_connection(path=world.path)
    try:
        result = getattr(passes, name)(conn, *args, **kwargs)
        conn.commit()
        return result
    finally:
        conn.close()


def live(world: World, shipment_id: str) -> sqlite3.Row:
    return query(world, "select p.* from dock_access_authorizations p join visit_shipments v using(truck_visit_id) "
                        "where v.shipment_id=? order by p.rowid desc limit 1", shipment_id)[0]


def everything(world: World) -> str:
    conn = open_connection(path=world.path)
    try:
        names = [r[0] for r in conn.execute("select name from sqlite_master where type='table'")]
        return normalise(" ".join(str(tuple(row)) for n in names for row in conn.execute(f'select * from "{n}"')))
    finally:
        conn.close()


def test_issue_binds_the_visit_and_stores_only_the_hash(world: World, made: str) -> None:
    code = issue(world, made)
    row = live(world, made)
    assert row["token_hash"] == hash_code(code) and row["status"] == "ISSUED"
    assert (row["driver_id"], row["carrier_id"], row["warehouse_id"]) == ("DRV001", "CAR001", "FAC-JAI-01")
    assert row["vehicle_registration"] == query(world, "select registration_number from vehicles where vehicle_id='VEH001'")[0][0]
    # 12:00-13:00 IST, an hour early (the facility's limit) to four hours after, in UTC.
    assert (row["valid_from_utc"], row["valid_until_utc"]) == ("2026-11-28T05:30:00+00:00", "2026-11-28T11:30:00+00:00")
    assert normalise(code)[3:] not in everything(world)
    visit = query(world, "select * from truck_visits where truck_visit_id=?", row["truck_visit_id"])[0]
    assert visit["status"] == "EXPECTED" and visit["vehicle_id"] == "VEH001"


def test_the_early_limit_rule_sets_the_start(world: World, made: str) -> None:
    query(world, "update facility_rules set rule_value='120' where facility_id='FAC-JAI-01' and rule_type='CHECKIN_EARLY_LIMIT_MIN'")
    issue(world, made)
    assert live(world, made)["valid_from_utc"] == "2026-11-28T04:30:00+00:00"


def test_a_reissue_leaves_one_live_pass_for_one_visit(world: World, made: str) -> None:
    first, second = issue(world, made), issue(world, made)
    assert first != second
    rows = query(world, "select status, token_hash, truck_visit_id from dock_access_authorizations order by rowid")
    assert len({r["truck_visit_id"] for r in rows}) == 1
    assert [r["status"] for r in rows][-2:] == ["REVOKED", "ISSUED"]
    assert sum(r["status"] == "ISSUED" for r in rows) == 1


def test_a_visit_in_progress_is_not_reissued(world: World, made: str) -> None:
    issue(world, made)
    query(world, "update truck_visits set status='CHECKED_IN'")
    with pytest.raises(AuthError, match="VISIT_IN_PROGRESS"):
        issue(world, made)


def test_revoke_needs_a_reason_and_the_right_warehouse(world: World, made: str) -> None:
    issue(world, made)
    for who, reason, code in (("wh_a", "  ", "REASON_REQUIRED"), ("wh_b", "lost", "SHIPMENT_NOT_FOUND")):
        with pytest.raises(AuthError, match=code):
            run(world, "revoke", actor=world.users[who], shipment_id=made, reason=reason, clock=world.clock)
    run(world, "revoke", actor=world.users["wh_a"], shipment_id=made, reason="lost", clock=world.clock)
    row = live(world, made)
    assert row["status"] == "REVOKED" and row["revocation_reason"] == "lost" and row["revoked_at"]
    with pytest.raises(AuthError, match="NO_LIVE_PASS"):
        run(world, "revoke", actor=world.users["wh_a"], shipment_id=made, reason="again", clock=world.clock)


def test_expiry_takes_waiting_passes_only(world: World, made: str) -> None:
    issue(world, made)
    assert run(world, "expire_due", world.clock) == 0
    world.clock.set(ts("2026-11-28T17:01:00+05:30"))
    query(world, "update dock_access_authorizations set status='ACTIVE' where status='ISSUED'")
    assert run(world, "expire_due", world.clock) == 0
    query(world, "update dock_access_authorizations set status='ISSUED' where status='ACTIVE'")
    assert run(world, "expire_due", world.clock) == 1
    assert live(world, made)["status"] == "EXPIRED"


def test_cancel_closes_pass_and_visit(world: World, made: str) -> None:
    issue(world, made)
    run(world, "cancel", made, world.clock)
    assert live(world, made)["status"] == "CANCELLED"
    assert query(world, "select status from truck_visits")[0][0] == "CANCELLED"


def test_a_shipment_without_an_appointment_gets_none(world: World, made: str) -> None:
    query(world, "update appointments set is_current=0 where shipment_id=?", made)
    with pytest.raises(AuthError, match="NO_APPOINTMENT"):
        issue(world, made)
    # Supplied shipments are not the app's to authorise.
    with pytest.raises(AuthError, match="SHIPMENT_NOT_FOUND"):
        issue(world, "SHP1001")
