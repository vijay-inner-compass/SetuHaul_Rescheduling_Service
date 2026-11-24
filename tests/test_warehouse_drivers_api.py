"""Warehouse driver list and Telegram revoke."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from setuhaul.auth import audit, onboarding
from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from tests.auth_helpers import World, build_world


@pytest.fixture()
def world(tmp_path: Path, clock: FakeClock) -> World:
    return build_world(tmp_path, clock)


def _add(world: World, who: str, name: str, phone: str, licence: str, days: int = 400) -> str:
    # An onboarded driver, added through the real flow by the named user.
    conn = open_connection(path=world.path)
    try:
        return onboarding.add_driver(conn, actor=world.users[who], name=name, phone=phone, licence_number=licence,
                                     licence_expiry=world.clock.now().date() + timedelta(days=days), carrier_id="CAR001",
                                     vehicle_id="VEH001", clock=world.clock)
    finally:
        conn.close()


def _bind(world: World, driver_id: str, revoked: bool = False) -> None:
    conn = open_connection(path=world.path)
    try:
        conn.execute("insert into telegram_identities(telegram_user_id, chat_id, driver_id, bound_at, revoked_at) values (?,?,?,?,?)",
                     (f"TG-{driver_id}", f"CH-{driver_id}", driver_id, world.clock.now().isoformat(),
                      world.clock.now().isoformat() if revoked else None))
        conn.commit()
    finally:
        conn.close()


def _by_id(resp) -> dict[str, dict]:  # type: ignore[no-untyped-def,type-arg]
    return {d["driver_id"]: d for d in resp.json()["drivers"]}


# ---- list --------------------------------------------------------------------------------------------------
def test_driver_list_roles(world: World) -> None:
    assert world.client().get("/v1/warehouse/drivers").status_code == 401
    assert world.client("wh_none").get("/v1/warehouse/drivers").status_code == 403
    assert world.client("wh_a").get("/v1/warehouse/drivers").status_code == 200
    assert world.client("admin").get("/v1/warehouse/drivers").status_code == 200


def test_a_warehouse_authority_sees_project_drivers_and_drivers_they_added(world: World) -> None:
    mine = _add(world, "wh_a", "Added By A", "+919811100001", "LIC-A-1")
    theirs = _add(world, "wh_b", "Added By B", "+919811100002", "LIC-B-1")
    got = _by_id(world.client("wh_a").get("/v1/warehouse/drivers"))
    # Own project driver (supplied) plus the one they added; nothing of B's.
    assert set(got) == {"DRV001", mine}
    assert theirs not in got and "DRV006" not in got
    assert set(_by_id(world.client("wh_b").get("/v1/warehouse/drivers"))) == {"DRV006", theirs}


def test_admin_sees_every_driver(world: World) -> None:
    added = _add(world, "wh_a", "Added By A", "+919811100001", "LIC-A-1")
    got = _by_id(world.client("admin").get("/v1/warehouse/drivers"))
    assert len(got) == 16 and added in got and {"DRV001", "DRV015"} <= set(got)


def test_empty_scope_is_an_empty_list_never_everything(world: World) -> None:
    # A warehouse authority with a facility but no project drivers and nobody added.
    conn = open_connection(path=world.path)
    try:
        conn.execute("update project_drivers set released_at=? where driver_id='DRV001'", (world.clock.now().isoformat(),))
        conn.commit()
    finally:
        conn.close()
    r = world.client("wh_a").get("/v1/warehouse/drivers")
    assert r.status_code == 200 and r.json()["drivers"] == []


def test_driver_fields_mask_phone_and_hide_licence_numbers(world: World) -> None:
    added = _add(world, "wh_a", "Added By A", "+919811100001", "LIC-SECRET-1", days=30)
    got = _by_id(world.client("wh_a").get("/v1/warehouse/drivers"))
    supplied, new = got["DRV001"], got[added]
    assert set(new) == {"driver_id", "driver_name", "phone", "carrier_id", "vehicle_id", "approval_status", "licence_expiry",
                        "licence_valid", "telegram_status"}
    # Phones are masked to the last four digits, whatever the stored form.
    assert supplied["phone"] == "+91••••••0001" and new["phone"] == "+91••••••0001"
    assert "LIC-SECRET-1" not in str(got) and "9811100001" not in str(got)
    # A supplied driver predates onboarding; an onboarded one is pending, so not valid to work yet.
    assert supplied["approval_status"] == "SUPPLIED" and supplied["licence_expiry"] is None and supplied["licence_valid"] is True
    assert new["approval_status"] == "PENDING_APPROVAL" and new["licence_valid"] is False
    assert new["licence_expiry"] == (world.clock.now().date() + timedelta(days=30)).isoformat()
    assert new["vehicle_id"] == "VEH001" and new["carrier_id"] == "CAR001"


def test_an_approved_driver_with_a_valid_licence_is_valid(world: World) -> None:
    added = _add(world, "wh_a", "Added By A", "+919811100001", "LIC-A-1")
    conn = open_connection(path=world.path)
    try:
        onboarding.approve(conn, actor=world.users["admin"], driver_id=added, clock=world.clock)
    finally:
        conn.close()
    assert _by_id(world.client("wh_a").get("/v1/warehouse/drivers"))[added]["licence_valid"] is True


def test_telegram_status_is_unbound_bound_or_revoked(world: World) -> None:
    second = _add(world, "wh_a", "Second", "+919811100003", "LIC-A-3")
    _bind(world, "DRV001")
    _bind(world, second, revoked=True)
    got = _by_id(world.client("wh_a").get("/v1/warehouse/drivers"))
    assert got["DRV001"]["telegram_status"] == "BOUND" and got[second]["telegram_status"] == "REVOKED"
    third = _add(world, "wh_a", "Third", "+919811100004", "LIC-A-4")
    assert _by_id(world.client("wh_a").get("/v1/warehouse/drivers"))[third]["telegram_status"] == "UNBOUND"


# ---- revoke ------------------------------------------------------------------------------------------------
def test_revoke_roles(world: World) -> None:
    path = "/v1/warehouse/drivers/DRV001/telegram/revoke"
    assert world.client().post(path).status_code == 401
    assert world.client("wh_none").post(path).status_code == 403


def test_a_warehouse_authority_revokes_a_driver_they_can_see(world: World) -> None:
    _bind(world, "DRV001")
    r = world.client("wh_a").post("/v1/warehouse/drivers/DRV001/telegram/revoke")
    assert r.status_code == 200 and r.json()["telegram_status"] == "REVOKED"
    conn = open_connection(path=world.path)
    try:
        assert [x["target_id"] for x in audit.query(conn, action="TELEGRAM_REVOKED", outcome="OK")] == ["DRV001"]
    finally:
        conn.close()
    assert _by_id(world.client("wh_a").get("/v1/warehouse/drivers"))["DRV001"]["telegram_status"] == "REVOKED"
    # Revoking again: nothing active is left to revoke.
    again = world.client("wh_a").post("/v1/warehouse/drivers/DRV001/telegram/revoke")
    assert again.status_code == 404 and again.json()["reason_code"] == "NOT_BOUND"


def test_revoke_outside_scope_is_404_and_changes_nothing(world: World) -> None:
    _bind(world, "DRV006")
    # wh_a cannot see wh_b's driver; the answer matches an unknown driver exactly.
    out = world.client("wh_a").post("/v1/warehouse/drivers/DRV006/telegram/revoke")
    unknown = world.client("wh_a").post("/v1/warehouse/drivers/DRV-NOPE/telegram/revoke")
    assert out.status_code == unknown.status_code == 404
    assert out.json()["reason_code"] == unknown.json()["reason_code"] == "DRIVER_NOT_FOUND"
    conn = open_connection(path=world.path)
    try:
        row = conn.execute("select revoked_at from telegram_identities where driver_id='DRV006'").fetchone()
    finally:
        conn.close()
    assert row["revoked_at"] is None
    # An admin can revoke any driver.
    assert world.client("admin").post("/v1/warehouse/drivers/DRV006/telegram/revoke").status_code == 200


def test_revoke_of_a_driver_with_no_binding_is_404(world: World) -> None:
    r = world.client("wh_a").post("/v1/warehouse/drivers/DRV001/telegram/revoke")
    assert r.status_code == 404 and r.json()["reason_code"] == "NOT_BOUND"


def test_licence_valid_in_the_list_matches_onboarding_for_every_state(world: World) -> None:
    today = world.clock.now().date()
    ok = _add(world, "wh_a", "Ok", "+919811100011", "LIC-11")
    expired = _add(world, "wh_a", "Exp", "+919811100012", "LIC-12")
    today_ok = _add(world, "wh_a", "Today", "+919811100013", "LIC-13")
    pending = _add(world, "wh_a", "Pend", "+919811100014", "LIC-14")
    rejected = _add(world, "wh_a", "Rej", "+919811100015", "LIC-15")
    conn = open_connection(path=world.path)
    try:
        for d in (ok, expired, today_ok, rejected):
            onboarding.approve(conn, actor=world.users["admin"], driver_id=d, clock=world.clock) if d != rejected else \
                onboarding.reject(conn, actor=world.users["admin"], driver_id=d, clock=world.clock)
        conn.execute("update driver_profiles set licence_expiry=? where driver_id=?", ((today - timedelta(days=1)).isoformat(), expired))
        conn.execute("update driver_profiles set licence_expiry=? where driver_id=?", (today.isoformat(), today_ok))
        conn.commit()
        want = {d: onboarding.licence_valid(conn, d, today) for d in (ok, expired, today_ok, pending, rejected, "DRV001")}
    finally:
        conn.close()
    assert want == {ok: True, expired: False, today_ok: True, pending: False, rejected: False, "DRV001": True}
    got = _by_id(world.client("wh_a").get("/v1/warehouse/drivers"))
    assert {d: got[d]["licence_valid"] for d in want} == want
