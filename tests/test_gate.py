"""Gate checks, check-in, unloading complete, check-out and what happens when a truck overstays."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from setuhaul import gate, passes
from setuhaul.db import open_connection
from setuhaul.db import parse_operational_timestamp as ts
from setuhaul.dock_pass import new_code
from setuhaul.identity import AuthError
from tests.gate_helpers import Yard
from tests.shipment_helpers import count, query


@pytest.fixture()
def yard(tmp_path: Path) -> Yard:
    return Yard(tmp_path)


def refused(yard: Yard, expected: str, name: str = "validate", who: str = "guard", **kwargs: object) -> None:
    with pytest.raises(AuthError) as error:
        yard.call(name, who, **kwargs)
    assert error.value.code == expected
    assert "ORD-" not in str(error.value) and "SHP-" not in str(error.value)


def test_validation_shows_the_visit_and_marks_the_pass(yard: Yard) -> None:
    view = yard.call("validate", vehicle_registration=yard.registration.lower().replace("-", " "),
                     licence_number=" " + yard.licence.lower())
    assert view.order_reference == "ORD-261127-001" and view.vehicle_registration == yard.registration and view.dock_code == "D1"
    assert view.valid_until.startswith("2026-11-28T17:00")
    assert yard.one("select status from dock_access_authorizations")[0] == "VALIDATED"
    assert count(yard.world, "audit_log", "action='GATE_CHECK' and outcome='OK'") == 1


@pytest.mark.parametrize(("setup", "kwargs", "code"), [
    (None, {"code": "DAP-AAAAA-AAAAA-AAAAA-AAAAA-AAAAAA"}, "PASS_NOT_VALID"),
    ("update dock_access_authorizations set status='REVOKED'", {}, "PASS_NOT_VALID"),
    ("update dock_access_authorizations set status='CANCELLED'", {}, "PASS_NOT_VALID"),
    ("update dock_access_authorizations set status='ACTIVE'", {}, "ALREADY_CHECKED_IN"),
    ("update appointments set is_current=0", {}, "NO_APPOINTMENT"),
    ("update drivers set driver_status='INACTIVE' where driver_id='DRV001'", {}, "DRIVER_NOT_ELIGIBLE"),
    (None, {"vehicle_registration": "XX00XX0000"}, "DETAILS_DO_NOT_MATCH"),
    (None, {"licence_number": "WRONG"}, "DETAILS_DO_NOT_MATCH"),
])
def test_each_refusal(yard: Yard, setup: str | None, kwargs: dict[str, str], code: str) -> None:
    if setup:
        query(yard.world, setup)
    refused(yard, code, **kwargs)
    refused(yard, code, "check_in", **kwargs)
    assert yard.one("select status from truck_visits")[0] == "EXPECTED"
    assert count(yard.world, "facility_checkins", f"shipment_id='{yard.shipment_id}'") == 0
    assert count(yard.world, "audit_log", f"action='GATE_CHECK' and outcome='REFUSED' and detail_json like '%{code}%'") == 2


def test_the_wrong_warehouse_and_the_wrong_time(yard: Yard) -> None:
    refused(yard, "WRONG_WAREHOUSE", who="guard_b")
    yard.world.clock.set(ts("2026-11-28T10:59:00+05:30"))  # type: ignore[attr-defined]
    refused(yard, "TOO_EARLY")
    yard.world.clock.set(ts("2026-11-28T17:01:00+05:30"))  # type: ignore[attr-defined]
    refused(yard, "TOO_LATE")
    query(yard.world, "update dock_access_authorizations set status='EXPIRED'")
    refused(yard, "TOO_LATE")


def test_only_gate_roles(yard: Yard) -> None:
    refused(yard, "FORBIDDEN", who="wh_a")


def test_check_in_records_the_arrival(yard: Yard) -> None:
    yard.call("check_in")
    assert yard.one("select status, activated_at from dock_access_authorizations")[0] == "ACTIVE"
    assert yard.one("select status from truck_visits")[0] == "CHECKED_IN"
    assert yard.one("select current_status from shipments where shipment_id=?", yard.shipment_id)[0] == "AT_GATE"
    row = yard.one("select * from facility_checkins where shipment_id=?", yard.shipment_id)
    assert row["gate_in_ts"].startswith("2026-11-28T11:45") and row["arrival_state"] == "ON_TIME" and row["gate_out_ts"] is None
    assert yard.one("select confirmed_at from shipment_assignments")[0] is not None


def test_the_same_press_twice_is_one_check_in(yard: Yard) -> None:
    first = yard.call("check_in")
    assert yard.call("check_in") == first
    assert count(yard.world, "audit_log", "action='CHECK_IN'") == 1
    refused(yard, "ALREADY_CHECKED_IN", "check_in", request_key="key-2")


def test_two_guards_at_once_make_one_check_in(yard: Yard) -> None:
    barrier, results = threading.Barrier(2), []

    def attempt(key: str) -> None:
        barrier.wait()
        try:
            yard.call("check_in", request_key=key)
            results.append("OK")
        except AuthError as error:
            results.append(error.code)

    threads = [threading.Thread(target=attempt, args=(k,)) for k in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["ALREADY_CHECKED_IN", "OK"]
    assert count(yard.world, "audit_log", "action='CHECK_IN'") == 1


def test_the_whole_visit(yard: Yard) -> None:
    visit = yard.call("check_in").truck_visit_id
    with pytest.raises(AuthError, match="UNLOADING_NOT_COMPLETE"):
        yard.call("check_out", truck_visit_id=visit, code=None, request_key="out-1")
    with pytest.raises(AuthError, match="VISIT_NOT_FOUND"):
        yard.call("unloading_complete", "wh_b", truck_visit_id=visit)
    yard.call("unloading_complete", "wh_a", truck_visit_id=visit)
    yard.call("unloading_complete", "wh_a", truck_visit_id=visit)
    assert yard.one("select appointment_status from appointments where shipment_id=?", yard.shipment_id)[0] == "COMPLETED"
    assert yard.one("select status from dock_access_authorizations")[0] == "COMPLETED"
    refused(yard, "PASS_NOT_VALID")
    with pytest.raises(AuthError, match="VISIT_NOT_FOUND"):
        yard.call("check_out", "guard_b", truck_visit_id=visit, code=None, request_key="out-1")
    yard.call("check_out", truck_visit_id=None, code=yard.code, request_key="out-1")
    yard.call("check_out", truck_visit_id=None, code=yard.code, request_key="out-1")
    assert yard.one("select status from truck_visits")[0] == "CHECKED_OUT"
    assert yard.one("select status from dock_access_authorizations")[0] == "CLOSED"
    assert yard.one("select current_status from shipments where shipment_id=?", yard.shipment_id)[0] == "COMPLETED"
    assert yard.one("select gate_out_ts from facility_checkins where shipment_id=?", yard.shipment_id)[0]
    assert count(yard.world, "audit_log", "action='CHECK_OUT'") == 1
    refused(yard, "PASS_NOT_VALID")
    with pytest.raises(AuthError, match="NOT_CHECKED_IN"):
        yard.call("check_out", truck_visit_id=visit, code=None, request_key="out-2")


def test_ten_failures_pause_the_guard(yard: Yard) -> None:
    for _ in range(10):
        refused(yard, "PASS_NOT_VALID", code=new_code())
    refused(yard, "TOO_MANY_ATTEMPTS")
    refused(yard, "TOO_MANY_ATTEMPTS", "check_in")
    assert yard.call("validate", "admin").order_reference == "ORD-261127-001"
    assert 0 < yard.redis.ttl(f"setuhaul:gate:{yard.world.users['guard'].user_id}") <= 900


def test_an_overstay_opens_one_case_and_releases_nothing(yard: Yard) -> None:
    visit = yard.call("check_in").truck_visit_id
    yard.world.clock.set(ts("2026-11-28T18:00:00+05:30"))  # type: ignore[attr-defined]
    conn = open_connection(path=yard.world.path)
    assert gate.sweep(conn, yard.world.clock) == 1 and gate.sweep(conn, yard.world.clock) == 0
    assert passes.expire_due(conn, yard.world.clock) == 0
    conn.close()
    assert count(yard.world, "escalation_cases", "reason_code='VISIT_OVERSTAY'") == 1
    assert yard.one("select status from truck_visits")[0] == "CHECKED_IN"
    assert yard.one("select appointment_status from appointments where shipment_id=?", yard.shipment_id)[0] == "CONFIRMED"
    conn = open_connection(path=yard.world.path)
    listed = gate.inside(conn, yard.world.users["guard"])
    assert [(v["truck_visit_id"], v["overstayed"]) for v in listed] == [(visit, True)]
    assert gate.inside(conn, yard.world.users["guard_b"]) == []
    conn.close()
