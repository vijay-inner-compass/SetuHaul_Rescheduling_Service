"""Customer-contract projects and driver allocation: facility scope, eligibility, release and visibility."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

from setuhaul.api import AppDeps, create_app
from setuhaul.auth import audit, onboarding, projects, sessions, users
from setuhaul.auth.authz import AuthzError
from setuhaul.auth.users import User
from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection
from setuhaul.identity import AuthError


class Env:
    # Everything a test needs: two facilities, an admin, two warehouse authorities (one per facility) and a third bound to none.
    def __init__(self, tmp_path: Path, clock: FakeClock) -> None:
        self.path = make_working_db(tmp_path / "p.sqlite3")
        self.conn = open_connection(path=self.path)
        self.clock = clock
        facs = [r[0] for r in self.conn.execute("select facility_id from facilities order by facility_id limit 2")]
        self.fac_a, self.fac_b = facs
        self.admin = users.bootstrap_admin(self.conn, "boss@x.com", clock)
        self.wh_a = users.invite(self.conn, actor=self.admin, email="a@x.com", role="WAREHOUSE_AUTHORITY",
                                 display_name="A", facilities=(self.fac_a,), clock=clock)
        self.wh_b = users.invite(self.conn, actor=self.admin, email="b@x.com", role="WAREHOUSE_AUTHORITY",
                                 display_name="B", facilities=(self.fac_b,), clock=clock)
        # Invite requires a facility, so one bound to none is made by unbinding afterwards.
        self.wh_none = users.invite(self.conn, actor=self.admin, email="n@x.com", role="WAREHOUSE_AUTHORITY",
                                    display_name="N", facilities=(self.fac_a,), clock=clock)
        self.conn.execute("delete from user_facilities where user_id=?", (self.wh_none.user_id,))
        self.conn.commit()
        self.wh_none = self.wh_none.model_copy(update={"facilities": ()})
        veh = self.conn.execute("select vehicle_id, carrier_id from vehicles where active_flag=1 order by vehicle_id").fetchone()
        self.carrier, self.vehicle = veh["carrier_id"], veh["vehicle_id"]
        self.n = 0

    def create(self, actor: User, facility: str | None = None, ref: str | None = None) -> str:
        self.n += 1
        return projects.create(self.conn, actor=actor, customer_name="Acme", contract_ref=ref or f"C-{self.n}",
                               facility_id=facility or actor.facilities[0], starts_on=self.clock.now().date(),
                               ends_on=None, clock=self.clock)

    def onboarded(self, approve: bool = True, days: int = 365) -> str:
        # An app-created driver; approved (active) when asked.
        self.n += 1
        did = onboarding.add_driver(
            self.conn, actor=self.wh_a, name="Ravi", phone=f"98765{10000 + self.n}", licence_number=f"LIC-{self.n}",
            licence_expiry=self.clock.now().date() + timedelta(days=days), carrier_id=self.carrier,
            vehicle_id=self.vehicle, clock=self.clock)
        if approve:
            onboarding.approve(self.conn, actor=self.admin, driver_id=did, clock=self.clock)
        return did


@pytest.fixture()
def env(tmp_path: Path, clock: FakeClock) -> Env:
    return Env(tmp_path, clock)


def _code(exc: pytest.ExceptionInfo[AuthError]) -> str:
    return exc.value.code


def test_warehouse_creates_project_in_own_facility_only(env: Env) -> None:
    pid = env.create(env.wh_a)
    assert pid.startswith("PRJ-")
    row = env.conn.execute("select * from projects where project_id=?", (pid,)).fetchone()
    assert row["facility_id"] == env.fac_a and row["status"] == "ACTIVE" and row["created_by"] == env.wh_a.user_id
    # Another facility is refused; so is a warehouse authority with none.
    with pytest.raises(AuthzError) as exc:
        env.create(env.wh_a, facility=env.fac_b)
    assert exc.value.status == 403
    with pytest.raises(AuthzError):
        env.create(env.wh_none, facility=env.fac_a)
    # An admin may create anywhere; an unknown facility is refused.
    assert env.create(env.admin, facility=env.fac_b)
    with pytest.raises(AuthError) as e2:
        env.create(env.admin, facility="NOPE")
    assert _code(e2) == "FACILITY_NOT_FOUND"
    # Two creations and two audited refusals.
    outcomes = sorted(r["outcome"] for r in audit.query(env.conn, action="PROJECT_CREATED"))
    assert outcomes == ["FORBIDDEN", "FORBIDDEN", "OK", "OK"]


def test_duplicate_contract_ref_refused(env: Env) -> None:
    env.create(env.wh_a, ref="DUP-1")
    with pytest.raises(AuthError) as exc:
        env.create(env.wh_b, ref="DUP-1")
    assert _code(exc) == "DUPLICATE_CONTRACT"


def test_create_validates_input(env: Env) -> None:
    d = env.clock.now().date()
    kw: dict[str, object] = {"actor": env.wh_a, "customer_name": "Acme", "contract_ref": "V-1",
                             "facility_id": env.fac_a, "starts_on": d, "ends_on": None, "clock": env.clock}
    for bad in ({"ends_on": d - timedelta(days=1)}, {"customer_name": " "}, {"customer_name": "A\x00"},
                {"contract_ref": ""}, {"contract_ref": "x" * 61}, {"customer_name": "x" * 121}):
        with pytest.raises(AuthError) as exc:
            projects.create(env.conn, **{**kw, **bad})  # type: ignore[arg-type]
        assert _code(exc) == "INVALID_REQUEST"
    # ends_on equal to starts_on is fine.
    assert projects.create(env.conn, **{**kw, "ends_on": d})  # type: ignore[arg-type]


def test_allocate_requires_approved_and_valid_licence(env: Env) -> None:
    pid = env.create(env.wh_a)
    # Pending onboarded driver: refused.
    pending = env.onboarded(approve=False)
    with pytest.raises(AuthError) as exc:
        projects.allocate(env.conn, actor=env.wh_a, project_id=pid, driver_id=pending, clock=env.clock)
    assert _code(exc) == "DRIVER_NOT_ELIGIBLE"
    # Approved driver whose licence has since expired: refused.
    expiring = env.onboarded(days=10)
    env.clock.advance(timedelta(days=11))
    with pytest.raises(AuthError) as e2:
        projects.allocate(env.conn, actor=env.wh_a, project_id=pid, driver_id=expiring, clock=env.clock)
    assert _code(e2) == "DRIVER_NOT_ELIGIBLE"
    # Unknown driver: refused the same way.
    with pytest.raises(AuthError) as e3:
        projects.allocate(env.conn, actor=env.wh_a, project_id=pid, driver_id="NOPE", clock=env.clock)
    assert _code(e3) == "DRIVER_NOT_ELIGIBLE"
    # Approved with a valid licence: allowed.
    ok = env.onboarded()
    projects.allocate(env.conn, actor=env.wh_a, project_id=pid, driver_id=ok, clock=env.clock)
    assert projects.drivers_of(env.conn, pid) == [ok]


def test_supplied_driver_can_be_allocated(env: Env) -> None:
    pid = env.create(env.wh_a)
    projects.allocate(env.conn, actor=env.wh_a, project_id=pid, driver_id="DRV001", clock=env.clock)
    assert projects.drivers_of(env.conn, pid) == ["DRV001"]
    # Twice is refused.
    with pytest.raises(AuthError) as exc:
        projects.allocate(env.conn, actor=env.wh_a, project_id=pid, driver_id="DRV001", clock=env.clock)
    assert _code(exc) == "ALREADY_ALLOCATED"


def test_cannot_allocate_across_facilities(env: Env) -> None:
    # A project in facility B is invisible to a facility-A authority: not found, not forbidden.
    pid = env.create(env.wh_b)
    with pytest.raises(AuthError) as exc:
        projects.allocate(env.conn, actor=env.wh_a, project_id=pid, driver_id="DRV001", clock=env.clock)
    assert _code(exc) == "PROJECT_NOT_FOUND"
    with pytest.raises(AuthError) as e2:
        projects.release(env.conn, actor=env.wh_a, project_id=pid, driver_id="DRV001", clock=env.clock)
    assert _code(e2) == "PROJECT_NOT_FOUND"
    # The same for a warehouse authority with no facilities, and for an unknown id.
    with pytest.raises(AuthError) as e3:
        projects.allocate(env.conn, actor=env.wh_none, project_id=pid, driver_id="DRV001", clock=env.clock)
    assert _code(e3) == "PROJECT_NOT_FOUND"
    with pytest.raises(AuthError) as e4:
        projects.allocate(env.conn, actor=env.wh_a, project_id="PRJ-NOPE", driver_id="DRV001", clock=env.clock)
    assert _code(e4) == "PROJECT_NOT_FOUND"


def test_other_warehouse_cannot_allocate_to_my_project(env: Env) -> None:
    # A second authority bound to the same facility can see the project but is not its creator.
    other = users.invite(env.conn, actor=env.admin, email="a2@x.com", role="WAREHOUSE_AUTHORITY", display_name="A2",
                         facilities=(env.fac_a,), clock=env.clock)
    pid = env.create(env.wh_a)
    with pytest.raises(AuthError) as exc:
        projects.allocate(env.conn, actor=other, project_id=pid, driver_id="DRV001", clock=env.clock)
    assert _code(exc) == "FORBIDDEN"
    # Nothing was allocated, and the refusal is audited.
    assert projects.drivers_of(env.conn, pid) == []
    assert any(r["outcome"] == "FORBIDDEN" for r in audit.query(env.conn, action="DRIVER_ALLOCATED"))
    # An admin may.
    projects.allocate(env.conn, actor=env.admin, project_id=pid, driver_id="DRV001", clock=env.clock)
    assert projects.drivers_of(env.conn, pid) == ["DRV001"]


def test_driver_can_be_on_two_projects(env: Env) -> None:
    p1, p2 = env.create(env.wh_a), env.create(env.wh_a)
    projects.allocate(env.conn, actor=env.wh_a, project_id=p1, driver_id="DRV001", clock=env.clock)
    projects.allocate(env.conn, actor=env.wh_a, project_id=p2, driver_id="DRV001", clock=env.clock)
    assert projects.drivers_of(env.conn, p1) == ["DRV001"] == projects.drivers_of(env.conn, p2)


def test_release_sets_released_at(env: Env) -> None:
    pid = env.create(env.wh_a)
    projects.allocate(env.conn, actor=env.wh_a, project_id=pid, driver_id="DRV001", clock=env.clock)
    projects.release(env.conn, actor=env.wh_a, project_id=pid, driver_id="DRV001", clock=env.clock)
    row = env.conn.execute("select released_at from project_drivers where project_id=?", (pid,)).fetchone()
    assert row["released_at"] == env.clock.now().isoformat()
    assert projects.drivers_of(env.conn, pid) == []
    # Releasing again, or a driver never allocated, is NOT_ALLOCATED.
    for did in ("DRV001", "DRV002"):
        with pytest.raises(AuthError) as exc:
            projects.release(env.conn, actor=env.wh_a, project_id=pid, driver_id=did, clock=env.clock)
        assert _code(exc) == "NOT_ALLOCATED"
    assert len(audit.query(env.conn, action="DRIVER_RELEASED")) == 1


def test_reallocate_after_release_keeps_history(env: Env) -> None:
    pid = env.create(env.wh_a)
    for step in ("allocate", "release", "allocate"):
        getattr(projects, step)(env.conn, actor=env.wh_a, project_id=pid, driver_id="DRV001", clock=env.clock)
        env.clock.advance(timedelta(minutes=1))
    assert projects.drivers_of(env.conn, pid) == ["DRV001"]
    # One row (the key is project+driver), active again; the trail holds the full history.
    rows = env.conn.execute("select released_at from project_drivers where project_id=?", (pid,)).fetchall()
    assert len(rows) == 1 and rows[0]["released_at"] is None
    actions = [r["action"] for r in audit.query(env.conn) if r["action"].startswith("DRIVER_")
               and r["target_id"] == "DRV001" and r["outcome"] == "OK"]
    assert sorted(actions) == ["DRIVER_ALLOCATED", "DRIVER_ALLOCATED", "DRIVER_RELEASED"]


def test_closed_project_refuses_allocation(env: Env) -> None:
    pid = env.create(env.wh_a)
    env.conn.execute("update projects set status='CLOSED' where project_id=?", (pid,))
    env.conn.commit()
    with pytest.raises(AuthError) as exc:
        projects.allocate(env.conn, actor=env.wh_a, project_id=pid, driver_id="DRV001", clock=env.clock)
    assert _code(exc) == "PROJECT_CLOSED"


def test_audit_detail_holds_ids_only(env: Env) -> None:
    pid = env.create(env.wh_a)
    projects.allocate(env.conn, actor=env.wh_a, project_id=pid, driver_id="DRV001", clock=env.clock)
    rows = audit.query(env.conn, action="DRIVER_ALLOCATED")
    assert rows[0]["target_id"] == "DRV001" and pid in rows[0]["detail_json"]
    assert "Acme" not in rows[0]["detail_json"]


def test_visibility_scoped_per_warehouse(env: Env) -> None:
    pa, pb = env.create(env.wh_a), env.create(env.wh_b)
    projects.allocate(env.conn, actor=env.wh_a, project_id=pa, driver_id="DRV001", clock=env.clock)
    projects.allocate(env.conn, actor=env.wh_b, project_id=pb, driver_id="DRV002", clock=env.clock)
    # Each authority sees only its own project's drivers; the admin sees all.
    assert projects.visible_driver_ids(env.conn, env.wh_a) == {"DRV001"}
    assert projects.visible_driver_ids(env.conn, env.wh_b) == {"DRV002"}
    assert projects.visible_driver_ids(env.conn, env.admin) == {"DRV001", "DRV002"}
    assert [p["project_id"] for p in projects.list_projects(env.conn, env.wh_a)] == [pa]
    assert [p["project_id"] for p in projects.list_projects(env.conn, env.wh_b)] == [pb]
    assert {p["project_id"] for p in projects.list_projects(env.conn, env.admin)} == {pa, pb}
    # Released drivers drop out of visibility.
    projects.release(env.conn, actor=env.wh_a, project_id=pa, driver_id="DRV001", clock=env.clock)
    assert projects.visible_driver_ids(env.conn, env.wh_a) == set()


def test_no_facilities_sees_nothing(env: Env) -> None:
    pid = env.create(env.admin, facility=env.fac_a)
    projects.allocate(env.conn, actor=env.admin, project_id=pid, driver_id="DRV001", clock=env.clock)
    # Even a project in a facility the user might once have had is invisible when they hold none.
    assert projects.list_projects(env.conn, env.wh_none) == []
    assert projects.visible_driver_ids(env.conn, env.wh_none) == set()


def test_visible_drivers_exclude_unbound_facility_and_non_creator(env: Env) -> None:
    # A project created by A is not visible to A once A is no longer bound to its facility.
    pid = env.create(env.wh_a)
    projects.allocate(env.conn, actor=env.wh_a, project_id=pid, driver_id="DRV001", clock=env.clock)
    moved = env.wh_a.model_copy(update={"facilities": (env.fac_b,)})
    assert projects.visible_driver_ids(env.conn, moved) == set()
    # A colleague in the same facility who did not create it does not see its drivers.
    other = env.wh_a.model_copy(update={"user_id": "USR-OTHER"})
    assert projects.visible_driver_ids(env.conn, other) == set()


def _client(env: Env, tmp_path: Path) -> tuple[TestClient, dict[str, str]]:
    tok = {"admin": sessions.create(env.conn, env.admin, "GOOGLE", env.clock),
           "a": sessions.create(env.conn, env.wh_a, "GOOGLE", env.clock),
           "b": sessions.create(env.conn, env.wh_b, "GOOGLE", env.clock),
           "none": sessions.create(env.conn, env.wh_none, "GOOGLE", env.clock)}
    env.conn.commit()
    deps = AppDeps(db_path=env.path, redis=fakeredis.FakeRedis(decode_responses=True), webhook_secret=b"w",
                   coordinator_tokens={}, fallback_path=tmp_path / "f.jsonl", clock=env.clock, session_secret=b"s" * 32)
    return TestClient(create_app(deps), base_url="http://localhost", follow_redirects=False), tok


def test_routes_full_flow(env: Env, tmp_path: Path) -> None:
    client, tok = _client(env, tmp_path)
    body = {"customer_name": "Acme", "contract_ref": "R-1", "facility_id": env.fac_a,
            "starts_on": "2026-08-01", "ends_on": "2026-12-31"}
    assert client.post("/v1/projects", json=body).status_code == 401
    client.cookies.set(sessions.COOKIE_NAME, tok["a"])
    r = client.post("/v1/projects", json=body)
    assert r.status_code == 201
    pid = r.json()["project_id"]
    # Duplicate contract, foreign facility, bad dates.
    assert client.post("/v1/projects", json=body).status_code == 409
    assert client.post("/v1/projects", json={**body, "contract_ref": "R-2", "facility_id": env.fac_b}).status_code == 403
    assert client.post("/v1/projects", json={**body, "contract_ref": "R-3", "ends_on": "2026-01-01"}).status_code == 422
    assert client.post("/v1/projects", json={**body, "contract_ref": "R-4", "extra": 1}).status_code == 422
    # Allocate, repeat, release.
    assert client.post(f"/v1/projects/{pid}/allocate", json={"driver_id": "DRV001"}).status_code == 200
    r = client.post(f"/v1/projects/{pid}/allocate", json={"driver_id": "DRV001"})
    assert r.status_code == 409 and r.json()["reason_code"] == "ALREADY_ALLOCATED"
    r = client.post(f"/v1/projects/{pid}/allocate", json={"driver_id": "NOPE"})
    assert r.status_code == 422 and r.json()["reason_code"] == "DRIVER_NOT_ELIGIBLE"
    listed = client.get("/v1/projects").json()["projects"]
    assert [p["project_id"] for p in listed] == [pid] and listed[0]["driver_ids"] == ["DRV001"]
    assert client.post(f"/v1/projects/{pid}/release", json={"driver_id": "DRV001"}).status_code == 200
    r = client.post(f"/v1/projects/{pid}/release", json={"driver_id": "DRV001"})
    assert r.status_code == 404 and r.json()["reason_code"] == "NOT_ALLOCATED"
    # Another facility's authority: 404 and an empty list; no facilities: 403 from the guard.
    client.cookies.set(sessions.COOKIE_NAME, tok["b"])
    r = client.post(f"/v1/projects/{pid}/allocate", json={"driver_id": "DRV001"})
    assert r.status_code == 404 and r.json()["reason_code"] == "PROJECT_NOT_FOUND"
    assert client.get("/v1/projects").json()["projects"] == []
    client.cookies.set(sessions.COOKIE_NAME, tok["none"])
    assert client.get("/v1/projects").status_code == 403
    client.cookies.set(sessions.COOKIE_NAME, tok["admin"])
    assert len(client.get("/v1/projects").json()["projects"]) == 1


def test_database_untouched_by_refused_create(env: Env) -> None:
    before: int = env.conn.execute("select count(*) from projects").fetchone()[0]
    with pytest.raises(AuthzError):
        env.create(env.wh_a, facility=env.fac_b)
    after: int = env.conn.execute("select count(*) from projects").fetchone()[0]
    assert before == after
    assert isinstance(env.conn, sqlite3.Connection)
