"""Driver onboarding: pending until an admin approves; licence rules; supplied rows untouched."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

from setuhaul.api import AppDeps, create_app
from setuhaul.auth import audit, onboarding, sessions, users
from setuhaul.auth.users import User
from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection
from setuhaul.identity import AuthError


def _env(tmp_path: Path, clock: FakeClock):  # type: ignore[no-untyped-def]
    # A database with an admin and a warehouse authority, plus a carrier and one of its active vehicles.
    path = make_working_db(tmp_path / "o.sqlite3")
    conn = open_connection(path=path)
    fac = conn.execute("select facility_id from facilities order by facility_id limit 1").fetchone()[0]
    admin = users.bootstrap_admin(conn, "boss@x.com", clock)
    wh = users.invite(conn, actor=admin, email="wh@x.com", role="WAREHOUSE_AUTHORITY", display_name="W",
                      facilities=(fac,), clock=clock)
    veh = conn.execute("select vehicle_id, carrier_id from vehicles where active_flag=1 order by vehicle_id").fetchone()
    return path, conn, admin, wh, veh["carrier_id"], veh["vehicle_id"]


def _add(conn: sqlite3.Connection, wh: User, clock: FakeClock, carrier: str, vehicle: str, **kw: object) -> str:
    args: dict[str, object] = {
        "name": "Ravi", "phone": "+91 90000 10099", "licence_number": "NEW-LIC-1",
        "licence_expiry": clock.now().date() + timedelta(days=365)}
    args.update(kw)
    return onboarding.add_driver(conn, actor=wh, carrier_id=carrier, vehicle_id=vehicle, clock=clock, **args)  # type: ignore[arg-type]


def _supplied(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    return [tuple(r) for r in conn.execute("select * from drivers where driver_id like 'DRV0%' order by driver_id limit 15")]


def test_warehouse_adds_driver_pending_and_inactive(tmp_path: Path, clock: FakeClock) -> None:
    _, conn, _, wh, car, veh = _env(tmp_path, clock)
    did = _add(conn, wh, clock, car, veh)
    assert did == "DRV016"
    d = conn.execute("select * from drivers where driver_id=?", (did,)).fetchone()
    assert d["driver_status"] == "INACTIVE" and d["phone"] == "+919000010099" and d["carrier_id"] == car
    p = conn.execute("select * from driver_profiles where driver_id=?", (did,)).fetchone()
    assert p["approval_status"] == "PENDING_APPROVAL" and p["created_by_user_id"] == wh.user_id and p["vehicle_id"] == veh
    # Ids keep counting up.
    assert _add(conn, wh, clock, car, veh, phone="9000010098", licence_number="NEW-LIC-2") == "DRV017"


def test_only_admin_approves(tmp_path: Path, clock: FakeClock) -> None:
    _, conn, admin, wh, car, veh = _env(tmp_path, clock)
    did = _add(conn, wh, clock, car, veh)
    with pytest.raises(AuthError) as e:
        onboarding.approve(conn, actor=wh, driver_id=did, clock=clock)
    assert e.value.code == "FORBIDDEN"
    assert conn.execute("select driver_status from drivers where driver_id=?", (did,)).fetchone()[0] == "INACTIVE"
    onboarding.approve(conn, actor=admin, driver_id=did, clock=clock)
    assert conn.execute("select driver_status from drivers where driver_id=?", (did,)).fetchone()[0] == "ACTIVE"
    p = conn.execute("select * from driver_profiles where driver_id=?", (did,)).fetchone()
    assert p["approval_status"] == "APPROVED" and p["approved_by"] == admin.user_id and p["approved_at"]


def test_warehouse_cannot_add_for_inactive_vehicle_or_foreign_carrier(tmp_path: Path, clock: FakeClock) -> None:
    _, conn, _, wh, car, veh = _env(tmp_path, clock)
    other = conn.execute("select carrier_id from carriers where carrier_id<>?", (car,)).fetchone()[0]
    with pytest.raises(AuthError) as e:
        _add(conn, wh, clock, other, veh)
    assert e.value.code == "VEHICLE_CARRIER_MISMATCH"
    conn.execute("update vehicles set active_flag=0 where vehicle_id=?", (veh,))
    with pytest.raises(AuthError) as e2:
        _add(conn, wh, clock, car, veh)
    assert e2.value.code == "VEHICLE_INACTIVE"


def test_reject_keeps_inactive_and_only_admin(tmp_path: Path, clock: FakeClock) -> None:
    _, conn, admin, wh, car, veh = _env(tmp_path, clock)
    did = _add(conn, wh, clock, car, veh)
    with pytest.raises(AuthError):
        onboarding.reject(conn, actor=wh, driver_id=did, clock=clock)
    onboarding.reject(conn, actor=admin, driver_id=did, clock=clock)
    assert conn.execute("select driver_status from drivers where driver_id=?", (did,)).fetchone()[0] == "INACTIVE"
    assert conn.execute("select approval_status from driver_profiles where driver_id=?", (did,)).fetchone()[0] == "REJECTED"
    # A rejected driver cannot be rejected again; an admin may re-approve them (suspension is reversible).
    with pytest.raises(AuthError) as e:
        onboarding.reject(conn, actor=admin, driver_id=did, clock=clock)
    assert e.value.code == "NOT_PENDING"
    onboarding.approve(conn, actor=admin, driver_id=did, clock=clock)
    assert conn.execute("select driver_status from drivers where driver_id=?", (did,)).fetchone()[0] == "ACTIVE"


def test_supplied_driver_rows_unchanged_by_onboarding(tmp_path: Path, clock: FakeClock) -> None:
    _, conn, admin, wh, car, veh = _env(tmp_path, clock)
    before = _supplied(conn)
    assert len(before) == 15
    did = _add(conn, wh, clock, car, veh)
    onboarding.approve(conn, actor=admin, driver_id=did, clock=clock)
    assert _supplied(conn) == before
    # Supplied drivers have no profile, so they can never be approved or rejected.
    for fn in (onboarding.approve, onboarding.reject):
        with pytest.raises(AuthError) as e:
            fn(conn, actor=admin, driver_id="DRV001", clock=clock)
        assert e.value.code == "NOT_PENDING"
    assert _supplied(conn) == before


def test_duplicate_licence_refused(tmp_path: Path, clock: FakeClock) -> None:
    _, conn, _, wh, car, veh = _env(tmp_path, clock)
    lic = conn.execute("select licence_number from drivers where driver_id='DRV001'").fetchone()[0]
    with pytest.raises(AuthError) as e:
        _add(conn, wh, clock, car, veh, licence_number=lic)
    assert e.value.code == "DUPLICATE_LICENCE"
    _add(conn, wh, clock, car, veh)
    with pytest.raises(AuthError) as e2:
        _add(conn, wh, clock, car, veh, phone="9000010055")
    assert e2.value.code == "DUPLICATE_LICENCE"


def test_duplicate_phone_matches_hyphenated_supplied_form(tmp_path: Path, clock: FakeClock) -> None:
    _, conn, _, wh, car, veh = _env(tmp_path, clock)
    # DRV001 is stored as '+91-9000010001'; '09000010001' is the same number.
    with pytest.raises(AuthError) as e:
        _add(conn, wh, clock, car, veh, phone="09000010001")
    assert e.value.code == "DUPLICATE_PHONE"
    with pytest.raises(AuthError) as e2:
        _add(conn, wh, clock, car, veh, phone="not a phone")
    assert e2.value.code == "INVALID_PHONE"
    # Nothing was written by the refusals.
    assert conn.execute("select count(*) from drivers").fetchone()[0] == 15


def test_expired_licence_blocks_approval(tmp_path: Path, clock: FakeClock) -> None:
    _, conn, admin, wh, car, veh = _env(tmp_path, clock)
    did = _add(conn, wh, clock, car, veh)
    # Time passes beyond the expiry before the admin gets to it.
    clock.advance(timedelta(days=400))
    with pytest.raises(AuthError) as e:
        onboarding.approve(conn, actor=admin, driver_id=did, clock=clock)
    assert e.value.code == "LICENCE_EXPIRED"
    assert conn.execute("select driver_status from drivers where driver_id=?", (did,)).fetchone()[0] == "INACTIVE"
    # An already-expired licence is also refused at add time.
    with pytest.raises(AuthError) as e2:
        _add(conn, wh, clock, car, veh, phone="9000010077", licence_number="L77", licence_expiry=date(2020, 1, 1))
    assert e2.value.code == "LICENCE_EXPIRED"


def test_licence_expiring_after_approval_fails_licence_valid(tmp_path: Path, clock: FakeClock) -> None:
    _, conn, admin, wh, car, veh = _env(tmp_path, clock)
    did = _add(conn, wh, clock, car, veh)
    # Pending drivers are not valid yet.
    assert onboarding.licence_valid(conn, did, clock.now().date()) is False
    onboarding.approve(conn, actor=admin, driver_id=did, clock=clock)
    assert onboarding.licence_valid(conn, did, clock.now().date()) is True
    expiry = clock.now().date() + timedelta(days=365)
    assert onboarding.licence_valid(conn, did, expiry) is True
    # Past the expiry date, with no admin action at all.
    clock.advance(timedelta(days=366))
    assert onboarding.licence_valid(conn, did, clock.now().date()) is False


def test_licence_valid_true_for_supplied_drivers_without_profile(tmp_path: Path, clock: FakeClock) -> None:
    _, conn, admin, wh, car, veh = _env(tmp_path, clock)
    assert onboarding.licence_valid(conn, "DRV001", clock.now().date()) is True
    # A rejected driver is not valid.
    did = _add(conn, wh, clock, car, veh)
    onboarding.reject(conn, actor=admin, driver_id=did, clock=clock)
    assert onboarding.licence_valid(conn, did, clock.now().date()) is False


def test_add_and_approve_are_audited(tmp_path: Path, clock: FakeClock) -> None:
    _, conn, admin, wh, car, veh = _env(tmp_path, clock)
    did = _add(conn, wh, clock, car, veh)
    onboarding.approve(conn, actor=admin, driver_id=did, clock=clock)
    did2 = _add(conn, wh, clock, car, veh, phone="9000010088", licence_number="NEW-LIC-9")
    onboarding.reject(conn, actor=admin, driver_id=did2, clock=clock)
    rows = {r["action"]: r for r in audit.query(conn)}
    assert {"DRIVER_ADDED", "DRIVER_APPROVED", "DRIVER_REJECTED"} <= set(rows)
    assert rows["DRIVER_APPROVED"]["target_id"] == did and rows["DRIVER_APPROVED"]["actor_user_id"] == admin.user_id
    assert rows["DRIVER_ADDED"]["actor_user_id"] == wh.user_id
    # No licence number or phone in the audit trail.
    dump = " ".join(str(tuple(r)) for r in audit.query(conn))
    assert "NEW-LIC" not in dump and "9000010099" not in dump


def test_concurrent_adds_get_distinct_ids(tmp_path: Path, clock: FakeClock) -> None:
    path, conn, _, wh, car, veh = _env(tmp_path, clock)
    other = open_connection(path=path)
    a = _add(conn, wh, clock, car, veh)
    b = _add(other, wh, clock, car, veh, phone="9000010066", licence_number="NEW-LIC-6")
    other.close()
    assert a != b


# ---- routes -------------------------------------------------------------------------------------------------

def _client(tmp_path: Path, clock: FakeClock):  # type: ignore[no-untyped-def]
    path, conn, admin, wh, car, veh = _env(tmp_path, clock)
    tok = {"admin": sessions.create(conn, admin, "GOOGLE", clock), "wh": sessions.create(conn, wh, "GOOGLE", clock)}
    conn.commit()
    conn.close()
    deps = AppDeps(db_path=path, redis=fakeredis.FakeRedis(decode_responses=True), webhook_secret=b"w",
                   coordinator_tokens={}, fallback_path=tmp_path / "f.jsonl", clock=clock, session_secret=b"s" * 32)
    client = TestClient(create_app(deps), base_url="http://localhost", follow_redirects=False)
    body = {"name": "Ravi", "phone": "9000010099", "licence_number": "NEW-LIC-1",
            "licence_expiry": (clock.now().date() + timedelta(days=300)).isoformat(), "carrier_id": car, "vehicle_id": veh}
    return client, tok, body


def _as(client: TestClient, token: str) -> None:
    client.cookies.set(sessions.COOKIE_NAME, token)


def test_routes_full_flow(tmp_path: Path, clock: FakeClock) -> None:
    client, tok, body = _client(tmp_path, clock)
    assert client.post("/v1/drivers", json=body).status_code == 401
    _as(client, tok["wh"])
    r = client.post("/v1/drivers", json=body)
    assert r.status_code == 201 and r.json()["driver_id"] == "DRV016"
    # A warehouse authority cannot list or approve.
    assert client.get("/v1/drivers/pending").status_code == 403
    assert client.post("/v1/drivers/DRV016/approve").status_code == 403
    _as(client, tok["admin"])
    pend = client.get("/v1/drivers/pending").json()["drivers"]
    assert [d["driver_id"] for d in pend] == ["DRV016"] and pend[0]["licence_expiry"] == body["licence_expiry"]
    assert client.post("/v1/drivers/DRV016/approve").status_code == 200
    assert client.get("/v1/drivers/pending").json()["drivers"] == []
    # Approving again, or deciding a supplied driver, is a 409; an unknown id is a 404.
    assert client.post("/v1/drivers/DRV016/approve").json()["reason_code"] == "NOT_PENDING"
    assert client.post("/v1/drivers/DRV001/approve").status_code == 409
    assert client.post("/v1/drivers/DRV999/approve").status_code == 404


def test_routes_reject_and_duplicates_and_validation(tmp_path: Path, clock: FakeClock) -> None:
    client, tok, body = _client(tmp_path, clock)
    _as(client, tok["admin"])
    assert client.post("/v1/drivers", json=body).status_code == 201
    dup_phone = {**body, "phone": "+91 90000 10099", "licence_number": "OTHER"}
    r = client.post("/v1/drivers", json=dup_phone)
    assert r.status_code == 409 and r.json()["reason_code"] == "DUPLICATE_PHONE"
    r = client.post("/v1/drivers", json={**body, "phone": "9000010001", "licence_number": "RJ14DL1001"})
    assert r.status_code == 409 and r.json()["reason_code"] in {"DUPLICATE_PHONE", "DUPLICATE_LICENCE"}
    r = client.post("/v1/drivers", json={**body, "phone": "9000010044", "licence_number": "RJ14DL1001"})
    assert r.status_code == 409 and r.json()["reason_code"] == "DUPLICATE_LICENCE"
    # Bad input is a 422 in the API's shape.
    bad = client.post("/v1/drivers", json={**body, "licence_expiry": "tomorrow"})
    assert bad.status_code == 422 and bad.json()["reason_code"] == "INVALID_REQUEST"
    assert client.post("/v1/drivers", json={**body, "vehicle_id": "NOPE", "phone": "9000010033",
                                           "licence_number": "Z"}).status_code == 422
    assert client.post("/v1/drivers/DRV016/reject").status_code == 200


# ---- review fixes ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("phone", ["0091 9000010001", "+91 09000010001"])
def test_prefix_variants_of_supplied_phone_are_duplicates(tmp_path: Path, clock: FakeClock, phone: str) -> None:
    _, conn, _, wh, car, veh = _env(tmp_path, clock)
    with pytest.raises(AuthError) as e:
        _add(conn, wh, clock, car, veh, phone=phone)
    assert e.value.code == "DUPLICATE_PHONE"


def test_licence_duplicate_ignores_spaces_and_hyphens(tmp_path: Path, clock: FakeClock) -> None:
    _, conn, _, wh, car, veh = _env(tmp_path, clock)
    for lic in ("RJ14 DL1001", "rj14-dl-1001"):
        with pytest.raises(AuthError) as e:
            _add(conn, wh, clock, car, veh, licence_number=lic)
        assert e.value.code == "DUPLICATE_LICENCE"


def test_name_and_expiry_bounds(tmp_path: Path, clock: FakeClock) -> None:
    _, conn, _, wh, car, veh = _env(tmp_path, clock)
    for name in ("Ra\x00vi", "Ra\nvi", "x" * 121):
        with pytest.raises(AuthError) as e:
            _add(conn, wh, clock, car, veh, name=name)
        assert e.value.code == "INVALID_NAME"
    with pytest.raises(AuthError) as e2:
        _add(conn, wh, clock, car, veh, licence_expiry=clock.now().date() + timedelta(days=366 * 21))
    assert e2.value.code == "INVALID_LICENCE_EXPIRY"
    assert _add(conn, wh, clock, car, veh, name="x" * 120)


def test_add_driver_refuses_unknown_role(tmp_path: Path, clock: FakeClock) -> None:
    _, conn, _, wh, car, veh = _env(tmp_path, clock)
    odd = User.model_construct(**{**wh.model_dump(), "role": "DRIVER"})
    with pytest.raises(AuthError) as e:
        onboarding.add_driver(conn, actor=odd, name="R", phone="9000010099", licence_number="L1",
                              licence_expiry=clock.now().date() + timedelta(days=5), carrier_id=car,
                              vehicle_id=veh, clock=clock)
    assert e.value.code == "FORBIDDEN"
    assert conn.execute("select count(*) from drivers").fetchone()[0] == 15


@pytest.mark.parametrize(("msg", "code"), [("UNIQUE constraint failed: drivers.phone", "DUPLICATE_PHONE"),
                                           ("UNIQUE constraint failed: drivers.licence_number", "DUPLICATE_LICENCE")])
def test_unique_backstop_is_409(tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch,
                                msg: str, code: str) -> None:
    client, tok, body = _client(tmp_path, clock)

    def boom(*a: object, **k: object) -> str:
        raise sqlite3.IntegrityError(msg)

    monkeypatch.setattr(onboarding, "add_driver", boom)
    _as(client, tok["wh"])
    r = client.post("/v1/drivers", json=body)
    assert r.status_code == 409 and r.json()["reason_code"] == code


# ---- hardening sweep (11c group C) ----

@pytest.mark.parametrize("msg", ["FOREIGN KEY constraint failed", "NOT NULL constraint failed: drivers.name",
                                 "CHECK constraint failed: x", "UNIQUE constraint failed: drivers.driver_id"])
def test_other_integrity_errors_are_not_409(tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, msg: str) -> None:
    client, tok, body = _client(tmp_path, clock)

    def boom(*a: object, **k: object) -> str:
        raise sqlite3.IntegrityError(msg)

    monkeypatch.setattr(onboarding, "add_driver", boom)
    _as(client, tok["wh"])
    r = client.post("/v1/drivers", json=body)
    assert r.status_code == 503 and r.json()["reason_code"] == "DATABASE_UNAVAILABLE"
