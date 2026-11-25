"""Stopping a driver: admin suspension of an approved driver, sticky Telegram revoke, audited re-link, and who may do it."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from setuhaul.auth import audit, onboarding, projects, telegram
from setuhaul.auth.users import User
from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from tests.auth_helpers import World, build_world


@pytest.fixture()
def world(tmp_path: Path, clock: FakeClock) -> World:
    return build_world(tmp_path, clock)


def _conn(world: World) -> sqlite3.Connection:
    return open_connection(path=world.path)


def _onboard(world: World, who: str = "wh_a", phone: str = "+91 90000 10099", approve: bool = True, days: int = 300) -> str:
    # A driver added by `who` through the real flow, approved by the admin unless asked otherwise.
    conn = _conn(world)
    try:
        did = onboarding.add_driver(conn, actor=world.users[who], name="Ravi", phone=phone, licence_number=f"LIC-{phone[-4:]}",
                                    licence_expiry=world.clock.now().date() + timedelta(days=days), carrier_id="CAR001",
                                    vehicle_id="VEH001", clock=world.clock)
        if approve:
            onboarding.approve(conn, actor=world.users["admin"], driver_id=did, clock=world.clock)
        return did
    finally:
        conn.close()


def _bind(world: World, phone: str, user_id: int) -> telegram.BindResult:
    conn = _conn(world)
    try:
        return telegram.bind_from_contact(conn, telegram_user_id=user_id, chat_id=user_id, contact_user_id=user_id,
                                          contact_phone=phone, clock=world.clock)
    finally:
        conn.close()


def _resolves(world: World, user_id: int) -> str | None:
    conn = _conn(world)
    try:
        return telegram.resolve_driver(conn, user_id, world.clock.now().date())
    finally:
        conn.close()


def _state(world: World, did: str) -> tuple[str, str]:
    conn = _conn(world)
    try:
        row = conn.execute("select d.driver_status, p.approval_status from drivers d join driver_profiles p using(driver_id)"
                           " where driver_id=?", (did,)).fetchone()
        return row["driver_status"], row["approval_status"]
    finally:
        conn.close()


def _audits(world: World, action: str) -> list[sqlite3.Row]:
    conn = _conn(world)
    try:
        return list(conn.execute("select * from audit_log where action=? order by audit_id", (action,)))
    finally:
        conn.close()


def _reasons(world: World) -> list[str]:
    return [json.loads(r["detail_json"])["reason"] for r in _audits(world, "TELEGRAM_BIND_REFUSED")]


# ---- (a) suspend and re-approve ----------------------------------------------------------------------------
def test_admin_suspends_an_approved_driver_and_bot_access_stops(world: World) -> None:
    did = _onboard(world)
    assert _bind(world, "+91 90000 10099", 111) == telegram.BindResult.BOUND
    r = world.client("admin").post(f"/v1/drivers/{did}/reject")
    assert r.status_code == 200 and r.json()["approval_status"] == "REJECTED"
    # Profile rejected and the row this feature inserted back to INACTIVE; the bot no longer resolves the driver.
    assert _state(world, did) == ("INACTIVE", "REJECTED")
    assert _resolves(world, 111) is None
    suspended = _audits(world, "DRIVER_SUSPENDED")
    assert [(a["target_id"], a["outcome"], a["actor_user_id"]) for a in suspended] == [(did, "OK", world.users["admin"].user_id)]
    # A pending driver's rejection is still a plain rejection.
    pending = _onboard(world, phone="+91 90000 10098", approve=False)
    assert world.client("admin").post(f"/v1/drivers/{pending}/reject").status_code == 200
    assert [a["target_id"] for a in _audits(world, "DRIVER_REJECTED")] == [pending]


def test_admin_re_approves_a_rejected_driver_subject_to_the_licence(world: World) -> None:
    did = _onboard(world, days=3)
    admin = world.client("admin")
    assert admin.post(f"/v1/drivers/{did}/reject").status_code == 200
    assert admin.post(f"/v1/drivers/{did}/approve").status_code == 200
    assert _state(world, did) == ("ACTIVE", "APPROVED")
    assert len(_audits(world, "DRIVER_APPROVED")) == 2
    # Suspended again, then the licence lapses: re-approval is refused.
    assert admin.post(f"/v1/drivers/{did}/reject").status_code == 200
    world.clock.advance(timedelta(days=4))
    # A fresh session: the earlier one expired during those days.
    r = world.client("admin").post(f"/v1/drivers/{did}/approve")
    assert r.status_code == 409 and r.json()["reason_code"] == "LICENCE_EXPIRED"
    assert _state(world, did) == ("INACTIVE", "REJECTED")


def test_only_an_admin_suspends_and_same_state_twice_is_refused(world: World) -> None:
    did = _onboard(world)
    assert world.client("wh_a").post(f"/v1/drivers/{did}/reject").status_code == 403
    assert world.client("admin").post(f"/v1/drivers/{did}/approve").json()["reason_code"] == "NOT_PENDING"
    assert world.client("admin").post(f"/v1/drivers/{did}/reject").status_code == 200
    assert world.client("admin").post(f"/v1/drivers/{did}/reject").json()["reason_code"] == "NOT_PENDING"


def test_supplied_drivers_are_never_suspended_or_updated(world: World) -> None:
    conn = _conn(world)
    before = conn.execute("select * from drivers where driver_id='DRV001'").fetchone()
    conn.close()
    r = world.client("admin").post("/v1/drivers/DRV001/reject")
    assert r.status_code == 409 and r.json()["reason_code"] == "NOT_PENDING"
    conn = _conn(world)
    try:
        assert tuple(conn.execute("select * from drivers where driver_id='DRV001'").fetchone()) == tuple(before)
    finally:
        conn.close()


# ---- (b) sticky revoke and allow re-link -------------------------------------------------------------------
def test_revoke_is_sticky_for_the_same_and_a_different_account(world: World) -> None:
    did = _onboard(world)
    assert _bind(world, "+91 90000 10099", 111) == telegram.BindResult.BOUND
    assert world.client("wh_a").post(f"/v1/warehouse/drivers/{did}/telegram/revoke").status_code == 200
    # Re-sharing the contact does not undo the revoke, from either account.
    assert _bind(world, "+91 90000 10099", 111) == telegram.BindResult.REFUSED
    assert _bind(world, "+91 90000 10099", 222) == telegram.BindResult.REFUSED
    assert _reasons(world) == ["REVOKED", "REVOKED"]
    assert _resolves(world, 111) is None and _resolves(world, 222) is None


def test_allow_relink_clears_the_revoke_and_is_audited(world: World) -> None:
    did = _onboard(world)
    _bind(world, "+91 90000 10099", 111)
    wh = world.client("wh_a")
    assert wh.post(f"/v1/warehouse/drivers/{did}/telegram/revoke").status_code == 200
    r = wh.post(f"/v1/warehouse/drivers/{did}/telegram/allow-relink")
    assert r.status_code == 200 and r.json()["telegram_status"] == "UNBOUND"
    assert [(a["target_id"], a["outcome"]) for a in _audits(world, "TELEGRAM_RELINK_ALLOWED")] == [(did, "OK")]
    # Now a (new) account may bind.
    assert _bind(world, "+91 90000 10099", 222) == telegram.BindResult.BOUND
    assert _resolves(world, 222) == did


def test_allow_relink_needs_a_revoked_binding(world: World) -> None:
    did = _onboard(world)
    wh = world.client("wh_a")
    unbound = wh.post(f"/v1/warehouse/drivers/{did}/telegram/allow-relink")
    assert unbound.status_code == 404 and unbound.json()["reason_code"] == "NOT_BOUND"
    _bind(world, "+91 90000 10099", 111)
    bound = wh.post(f"/v1/warehouse/drivers/{did}/telegram/allow-relink")
    assert bound.status_code == 409 and bound.json()["reason_code"] == "NOT_REVOKED"
    assert _resolves(world, 111) == did


def test_an_account_revoked_for_one_driver_cannot_bind_another_and_keeps_the_revoke(world: World) -> None:
    first = _onboard(world)
    second = _onboard(world, phone="+91 90000 10098")
    _bind(world, "+91 90000 10099", 111)
    assert world.client("wh_a").post(f"/v1/warehouse/drivers/{first}/telegram/revoke").status_code == 200
    # The same account now shares the other driver's number: refused, and the first driver's revoke is untouched.
    assert _bind(world, "+91 90000 10098", 111) == telegram.BindResult.REFUSED
    assert _reasons(world) == ["SENDER_REVOKED"]
    conn = _conn(world)
    try:
        row = conn.execute("select telegram_user_id, revoked_at from telegram_identities where driver_id=?", (first,)).fetchone()
        assert row["telegram_user_id"] == "111" and row["revoked_at"] is not None
        assert conn.execute("select 1 from telegram_identities where driver_id=?", (second,)).fetchone() is None
    finally:
        conn.close()
    assert _bind(world, "+91 90000 10099", 333) == telegram.BindResult.REFUSED


def test_supplied_driver_revoke_is_sticky_too(world: World) -> None:
    _bind(world, "+91 90000 10006", 111)
    admin = world.client("admin")
    assert admin.post("/v1/warehouse/drivers/DRV006/telegram/revoke").status_code == 200
    assert _bind(world, "+91 90000 10006", 111) == telegram.BindResult.REFUSED
    assert admin.post("/v1/warehouse/drivers/DRV006/telegram/allow-relink").status_code == 200
    assert _bind(world, "+91 90000 10006", 111) == telegram.BindResult.BOUND


# ---- (c) who may revoke and allow re-link ------------------------------------------------------------------
def _allocate(world: World, who: str, driver_id: str) -> None:
    conn = _conn(world)
    try:
        pid = conn.execute("select project_id from projects where created_by=?", (world.users[who].user_id,)).fetchone()[0]
        projects.allocate(conn, actor=world.users[who], project_id=pid, driver_id=driver_id, clock=world.clock)
    finally:
        conn.close()


@pytest.mark.parametrize("action", ["revoke", "allow-relink"])
def test_allocation_does_not_grant_revoke_or_relink(world: World, action: str) -> None:
    # wh_b's driver, allocated to wh_a's project; and a supplied driver on wh_a's own project.
    theirs = _onboard(world, who="wh_b")
    _allocate(world, "wh_a", theirs)
    for did, uid in ((theirs, 111), ("DRV001", 222)):
        conn = _conn(world)
        conn.execute("insert into telegram_identities(telegram_user_id, chat_id, driver_id, bound_at, revoked_at) values (?,?,?,?,?)",
                     (str(uid), str(uid), did, "x", "y" if action == "allow-relink" else None))
        conn.commit()
        conn.close()
        out = world.client("wh_a").post(f"/v1/warehouse/drivers/{did}/telegram/{action}")
        unknown = world.client("wh_a").post(f"/v1/warehouse/drivers/DRV-NOPE/telegram/{action}")
        assert out.status_code == unknown.status_code == 404
        assert out.json()["reason_code"] == unknown.json()["reason_code"] == "DRIVER_NOT_FOUND"
    # The creator and the admin may.
    assert world.client("wh_b").post(f"/v1/warehouse/drivers/{theirs}/telegram/{action}").status_code == 200
    assert world.client("admin").post(f"/v1/warehouse/drivers/DRV001/telegram/{action}").status_code == 200


def test_module_functions_enforce_the_same_rule(world: World) -> None:
    did = _onboard(world, who="wh_b")
    _bind(world, "+91 90000 10099", 111)
    conn = _conn(world)
    try:
        wh_a: User = world.users["wh_a"]
        with pytest.raises(telegram.AuthError) as exc:
            telegram.revoke(conn, actor=wh_a, driver_id=did, clock=world.clock)
        assert exc.value.code == "DRIVER_NOT_FOUND"
        telegram.revoke(conn, actor=world.users["wh_b"], driver_id=did, clock=world.clock)
        with pytest.raises(telegram.AuthError) as exc:
            telegram.allow_relink(conn, actor=wh_a, driver_id=did, clock=world.clock)
        assert exc.value.code == "DRIVER_NOT_FOUND"
        # The out-of-scope probes are audited.
        probes = [a["outcome"] for a in audit.query(conn, actor=wh_a.email)]
        assert probes.count("NOT_FOUND") == 2
    finally:
        conn.close()


# ---- (d) screens -------------------------------------------------------------------------------------------
def test_warehouse_page_offers_revoke_and_relink_only_where_permitted(world: World) -> None:
    mine = _onboard(world)
    conn = _conn(world)
    conn.execute("insert into telegram_identities(telegram_user_id, chat_id, driver_id, bound_at) values ('1','1','DRV001','x')")
    conn.execute("insert into telegram_identities(telegram_user_id, chat_id, driver_id, bound_at) values ('2','2',?,'x')", (mine,))
    conn.commit()
    conn.close()
    wh = world.client("wh_a")
    page = wh.get("/warehouse").text
    # Their own onboarded driver: Revoke. The supplied project driver: nothing (admin only).
    assert f"/v1/warehouse/drivers/{mine}/telegram/revoke" in page
    assert "/v1/warehouse/drivers/DRV001/telegram/revoke" not in page
    assert wh.post(f"/v1/warehouse/drivers/{mine}/telegram/revoke").status_code == 200
    page = wh.get("/warehouse").text
    assert f"/v1/warehouse/drivers/{mine}/telegram/allow-relink" in page and f"/v1/warehouse/drivers/{mine}/telegram/revoke" not in page
    # The admin sees Revoke for the supplied driver.
    assert "/v1/warehouse/drivers/DRV001/telegram/revoke" in world.client("admin").get("/warehouse").text


def test_admin_page_offers_suspend_and_re_approve(world: World) -> None:
    approved = _onboard(world)
    rejected = _onboard(world, phone="+91 90000 10098")
    admin = world.client("admin")
    assert admin.post(f"/v1/drivers/{rejected}/reject").status_code == 200
    page = admin.get("/admin").text
    # Suspend (the reject endpoint) for the approved driver, Re-approve for the rejected one; none for supplied drivers.
    assert f'data-api="/v1/drivers/{approved}/reject"' in page and "Suspend" in page
    assert f'data-api="/v1/drivers/{rejected}/approve"' in page and "Re-approve" in page
    assert '/v1/drivers/DRV001/' not in page
