"""Admin users and audit APIs."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from datetime import UTC, timedelta
from pathlib import Path

import pytest

from setuhaul.auth import admin_api, audit, users
from setuhaul.auth.admin_api import check_disable
from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from setuhaul.identity import AuthError
from tests.auth_helpers import GURUGRAM, JAIPUR, World, add_user, build_world


@pytest.fixture()
def world(tmp_path: Path, clock: FakeClock) -> World:
    return build_world(tmp_path, clock)


def _invite(world: World, who: str | None = "admin", **over: object):  # type: ignore[no-untyped-def]
    body = {"email": "new@x.com", "role": "WAREHOUSE_AUTHORITY", "display_name": "New", "facilities": [JAIPUR], **over}
    return world.client(who).post("/v1/admin/users", json=body)


# ---- roles -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("method", "path"), [("POST", "/v1/admin/users"), ("GET", "/v1/admin/users"),
                                              ("POST", "/v1/admin/users/USR-BOSS/disable"), ("GET", "/v1/admin/audit")])
def test_admin_routes_are_admin_only(world: World, method: str, path: str) -> None:
    kw = {"json": {}} if method == "POST" else {}
    assert world.client().request(method, path, **kw).status_code == 401
    assert world.client("wh_a").request(method, path, **kw).status_code == 403
    assert world.client("wh_none").request(method, path, **kw).status_code == 403


# ---- invite and list ---------------------------------------------------------------------------------------
def test_invite_creates_a_user_and_audits_it(world: World) -> None:
    r = _invite(world)
    assert r.status_code == 201
    user = r.json()["user"]
    assert user["email"] == "new@x.com" and user["role"] == "WAREHOUSE_AUTHORITY" and user["status"] == "ACTIVE"
    assert user["facilities"] == [JAIPUR] and user["display_name"] == "New"
    conn = open_connection(path=world.path)
    try:
        rows = audit.query(conn, action="INVITE", outcome="OK")
    finally:
        conn.close()
    assert [x["target_id"] for x in rows] == [user["user_id"]]


@pytest.mark.parametrize(("over", "status", "code"), [
    ({"email": "nonsense"}, 422, "INVALID_EMAIL"),
    ({"facilities": []}, 422, "FACILITY_REQUIRED"),
    ({"facilities": ["FAC-NOPE"]}, 422, "UNKNOWN_FACILITY"),
    ({"email": "wha@x.com"}, 409, "ALREADY_INVITED"),
])
def test_invite_errors_map_to_their_codes(world: World, over: dict[str, object], status: int, code: str) -> None:
    r = _invite(world, **over)
    assert r.status_code == status and r.json()["reason_code"] == code


def test_invite_rejects_unknown_roles_and_fields(world: World) -> None:
    assert _invite(world, role="SUPERUSER").status_code == 422
    assert _invite(world, user_id="USR-X").status_code == 422


def test_list_users_shows_everyone_without_secrets(world: World) -> None:
    r = world.client("admin").get("/v1/admin/users")
    assert r.status_code == 200
    listed = {u["email"]: u for u in r.json()["users"]}
    assert set(listed) == {"boss@x.com", "wha@x.com", "whb@x.com", "whnone@x.com"}
    one = listed["wha@x.com"]
    assert set(one) == {"user_id", "email", "role", "status", "display_name", "facilities", "last_login_at"}
    assert one["facilities"] == [JAIPUR] and listed["whb@x.com"]["facilities"] == [GURUGRAM]


# ---- disable -----------------------------------------------------------------------------------------------
def test_disable_blocks_the_user_and_is_audited(world: World) -> None:
    target = world.users["wh_a"]
    victim = world.client("wh_a")
    assert victim.get("/v1/warehouse/partner-requests").status_code == 200
    r = world.client("admin").post(f"/v1/admin/users/{target.user_id}/disable")
    assert r.status_code == 200 and r.json()["status"] == "OK"
    # The live session died with it.
    assert victim.get("/v1/warehouse/partner-requests").status_code == 401
    conn = open_connection(path=world.path)
    try:
        assert [x["target_id"] for x in audit.query(conn, action="DISABLE", outcome="OK")] == [target.user_id]
    finally:
        conn.close()


def test_disable_unknown_user_is_404(world: World) -> None:
    r = world.client("admin").post("/v1/admin/users/USR-NOPE/disable")
    assert r.status_code == 404 and r.json()["reason_code"] == "UNKNOWN_USER"


def test_an_admin_cannot_disable_themselves(world: World) -> None:
    admin = world.client("admin")
    r = admin.post(f"/v1/admin/users/{world.users['admin'].user_id}/disable")
    assert r.status_code == 409 and r.json()["reason_code"] == "CANNOT_DISABLE_SELF"
    # Still signed in and still active.
    assert admin.get("/v1/admin/users").status_code == 200


def test_the_last_active_admin_cannot_be_disabled(world: World, clock: FakeClock) -> None:
    conn = open_connection(path=world.path)
    try:
        sole = world.users["admin"]
        # A different (stale) actor object against a database holding exactly one active admin.
        ghost = sole.model_copy(update={"user_id": "USR-GHOST"})
        with pytest.raises(AuthError) as exc:
            check_disable(conn, actor=ghost, target_id=sole.user_id)
        assert exc.value.code == "LAST_ADMIN"
        # With a second active admin the same disable is allowed, and disabling a non-admin never trips the guard.
        other = add_user(conn, clock, "boss2@x.com", "ADMIN")
        check_disable(conn, actor=ghost, target_id=sole.user_id)
        check_disable(conn, actor=other, target_id=world.users["wh_a"].user_id)
        # A disabled admin does not count as active.
        users.disable(conn, actor=sole, user_id=other.user_id, clock=clock)
        with pytest.raises(AuthError):
            check_disable(conn, actor=ghost, target_id=sole.user_id)
    finally:
        conn.close()


# ---- audit -------------------------------------------------------------------------------------------------
def _seed_audit(world: World) -> None:
    conn = open_connection(path=world.path)
    try:
        for i, (actor, action, outcome) in enumerate([("a@x.com", "LOGIN", "OK"), ("b@x.com", "LOGIN", "DENIED"),
                                                      ("a@x.com", "INVITE", "OK")]):
            audit.record(conn, actor_user_id=f"U{i}", actor_email=actor, action=action, target_type="user", target_id=None,
                         outcome=outcome, ip="1.2.3.4", detail={"n": i, "token": "SECRET-TOKEN"}, clock=world.clock)
        conn.commit()
    finally:
        conn.close()


def test_audit_filters_and_hides_secrets(world: World) -> None:
    _seed_audit(world)
    admin = world.client("admin")
    everything = admin.get("/v1/admin/audit").json()["entries"]
    assert len(everything) >= 3 and "SECRET-TOKEN" not in str(everything)
    assert set(everything[0]) == {"audit_id", "ts", "actor_user_id", "actor_email", "action", "target_type", "target_id",
                                  "outcome", "ip", "detail"}
    assert [e["audit_id"] for e in everything] == sorted((e["audit_id"] for e in everything), reverse=True)
    got = admin.get("/v1/admin/audit", params={"actor": "a@x.com", "action": "LOGIN", "outcome": "OK"}).json()["entries"]
    assert len(got) == 1 and got[0]["actor_email"] == "a@x.com" and got[0]["detail"] == {"n": 0}
    assert len(admin.get("/v1/admin/audit", params={"limit": 1}).json()["entries"]) == 1


def test_audit_limit_is_capped_and_floored(world: World) -> None:
    admin = world.client("admin")
    assert admin.get("/v1/admin/audit", params={"limit": 100000}).status_code == 200
    # A non-positive limit falls back to the default rather than returning nothing or everything.
    assert admin.get("/v1/admin/audit", params={"limit": 0}).status_code == 200
    assert admin_api.MAX_AUDIT_LIMIT == 500


@pytest.mark.parametrize("since", ["yesterday", "2026-08-04T09:00:00", "2026-08-04", ""])
def test_audit_since_must_be_iso_with_an_offset(world: World, since: str) -> None:
    r = world.client("admin").get("/v1/admin/audit", params={"since": since})
    assert r.status_code == 422 and r.json()["reason_code"] == "INVALID_SINCE"


def test_audit_since_is_compared_in_ist(world: World) -> None:
    _seed_audit(world)
    admin = world.client("admin")
    now = world.clock.now()
    # The same instant written in UTC, in IST and with Z must select the same rows.
    utc = now.astimezone(UTC)
    results = []
    for stamp in (now.isoformat(), utc.isoformat(), utc.strftime("%Y-%m-%dT%H:%M:%S") + "Z"):
        r = admin.get("/v1/admin/audit", params={"since": stamp})
        assert r.status_code == 200, stamp
        results.append([e["audit_id"] for e in r.json()["entries"]])
    assert results[0] == results[1] == results[2] and results[0]
    # An hour later matches nothing, even though UTC clock digits sort lower than IST ones.
    later = (now + timedelta(hours=1)).astimezone(UTC).isoformat()
    assert admin.get("/v1/admin/audit", params={"since": later}).json()["entries"] == []
    # An hour earlier matches everything the fixed time did and more.
    earlier = (now - timedelta(hours=1)).astimezone(UTC).isoformat()
    assert len(admin.get("/v1/admin/audit", params={"since": earlier}).json()["entries"]) >= len(results[0])
