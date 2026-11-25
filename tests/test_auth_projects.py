"""Customer-contract projects and driver allocation: facility scope, eligibility, release and visibility."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import shutil
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

from setuhaul.api import AppDeps, create_app
from setuhaul.auth import audit, onboarding, projects, sessions, users
from setuhaul.auth.authz import AuthzError
from setuhaul.auth.users import User
from setuhaul.clock import FakeClock
from setuhaul.config import load_settings
from setuhaul.db import MIGRATIONS_DIR, apply_migrations, make_working_db, open_connection
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
                {"contract_ref": ""}, {"contract_ref": "x" * 65}, {"customer_name": "x" * 121}):
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


# ---- hardening sweep (11c group D) ----

def _kw(env: Env, **over: object) -> dict[str, object]:
    return {"actor": env.wh_a, "customer_name": "Acme", "contract_ref": "V-1", "facility_id": env.fac_a,
            "starts_on": env.clock.now().date(), "ends_on": None, "clock": env.clock, **over}


def _other_authority(env: Env) -> User:
    # A second authority in facility A: it can see A's projects but did not create them.
    return users.invite(env.conn, actor=env.admin, email="a2@x.com", role="WAREHOUSE_AUTHORITY", display_name="A2",
                        facilities=(env.fac_a,), clock=env.clock)


@pytest.mark.parametrize("action", ["allocate", "release"])
def test_refusal_rolls_back_a_partial_write(env: Env, monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    other = _other_authority(env)
    pid = env.create(env.wh_a)
    real = projects._load_for_change

    def write_then_check(conn: sqlite3.Connection, *args: object) -> sqlite3.Row:
        # A write that happens inside the same transaction, before the refusal.
        conn.execute("update projects set customer_name='PARTIAL' where project_id=?", (pid,))
        return real(conn, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(projects, "_load_for_change", write_then_check)
    with pytest.raises(AuthError) as exc:
        getattr(projects, action)(env.conn, actor=other, project_id=pid, driver_id="DRV001", clock=env.clock)
    assert _code(exc) == "FORBIDDEN"
    reader = open_connection(path=env.path, readonly=True)
    try:
        # The partial write is gone; the refusal itself is still audited.
        assert reader.execute("select customer_name from projects where project_id=?", (pid,)).fetchone()[0] == "Acme"
        assert [r["outcome"] for r in audit.query(reader) if r["target_id"] == pid and r["outcome"] == "FORBIDDEN"] == ["FORBIDDEN"]
    finally:
        reader.close()


@pytest.mark.parametrize("action", ["allocate", "release"])
def test_cross_facility_probe_is_audited(env: Env, action: str) -> None:
    pid = env.create(env.wh_b)
    with pytest.raises(AuthError) as exc:
        getattr(projects, action)(env.conn, actor=env.wh_a, project_id=pid, driver_id="DRV001", clock=env.clock)
    assert _code(exc) == "PROJECT_NOT_FOUND"
    rows = audit.query(env.conn, action="PROJECT_PROBE")
    assert [(r["outcome"], r["target_id"], r["actor_user_id"]) for r in rows] == [("NOT_FOUND", pid, env.wh_a.user_id)]
    assert not env.conn.in_transaction


def test_genuinely_missing_id_is_not_a_probe(env: Env) -> None:
    for actor in (env.wh_a, env.admin, env.wh_none):
        with pytest.raises(AuthError):
            projects.allocate(env.conn, actor=actor, project_id="PRJ-NOPE", driver_id="DRV001", clock=env.clock)
    assert audit.query(env.conn, action="PROJECT_PROBE") == []


def test_probe_target_is_truncated_and_response_identical(env: Env, tmp_path: Path) -> None:
    # A very long id that really exists outside the caller's scope.
    long_id = "PRJ-" + "L" * 100
    env.conn.execute("insert into projects(project_id, customer_name, contract_ref, facility_id, status, created_by)"
                     " values (?,?,?,?,'ACTIVE',?)", (long_id, "Acme", "LONG-1", env.fac_b, env.wh_b.user_id))
    env.conn.commit()
    client, tok = _client(env, tmp_path)
    client.cookies.set(sessions.COOKIE_NAME, tok["a"])
    hidden = client.post(f"/v1/projects/{long_id}/allocate", json={"driver_id": "DRV001"}, headers={"x-request-id": "r"})
    missing = client.post("/v1/projects/PRJ-NOPE/allocate", json={"driver_id": "DRV001"}, headers={"x-request-id": "r"})
    assert (hidden.status_code, hidden.json()) == (missing.status_code, missing.json())
    rows = audit.query(env.conn, action="PROJECT_PROBE")
    assert [r["target_id"] for r in rows] == [long_id[:64]]


@pytest.mark.parametrize("ref", ["A", "ab.C_d/e-1", "9" * 64])
def test_contract_ref_pattern_accepts(env: Env, ref: str) -> None:
    assert projects.create(env.conn, **_kw(env, contract_ref=ref))  # type: ignore[arg-type]


@pytest.mark.parametrize("ref", ["-A1", ".A", "/A", "A B", "A#1", "A?1", "caf\u00e9", "x" * 65])
def test_contract_ref_pattern_refuses(env: Env, ref: str) -> None:
    with pytest.raises(AuthError) as exc:
        projects.create(env.conn, **_kw(env, contract_ref=ref))  # type: ignore[arg-type]
    assert _code(exc) == "INVALID_REQUEST"


def test_contract_ref_unique_ignoring_case(env: Env) -> None:
    env.create(env.wh_a, ref="Dup-1")
    with pytest.raises(AuthError) as exc:
        env.create(env.wh_b, ref="dUP-1")
    assert _code(exc) == "DUPLICATE_CONTRACT"
    # The database enforces it too.
    with pytest.raises(sqlite3.IntegrityError, match="contract"):
        env.conn.execute("insert into projects(project_id, customer_name, contract_ref, facility_id, status, created_by)"
                         " values ('PRJ-X','A','DUP-1',?,'ACTIVE',?)", (env.fac_a, env.wh_a.user_id))
    env.conn.rollback()


@pytest.mark.parametrize(("starts", "ends"), [("1999-12-31", None), ("2101-01-01", None), ("2026-01-01", "2101-01-01"),
                                              ("1999-01-01", "1999-06-01")])
def test_dates_outside_range_refused(env: Env, starts: str, ends: str | None) -> None:
    with pytest.raises(AuthError) as exc:
        projects.create(env.conn, **_kw(env, starts_on=date.fromisoformat(starts),  # type: ignore[arg-type]
                                        ends_on=date.fromisoformat(ends) if ends else None))
    assert _code(exc) == "INVALID_REQUEST"


def test_date_range_edges_accepted(env: Env) -> None:
    assert projects.create(env.conn, **_kw(env, starts_on=date(2000, 1, 1), ends_on=date(2100, 12, 31)))  # type: ignore[arg-type]


def test_ended_project_refuses_allocation_by_ist_date(env: Env, tmp_path: Path) -> None:
    today = env.clock.now().date()
    pid = projects.create(env.conn, **_kw(env, ends_on=today))  # type: ignore[arg-type]
    # The last day still counts.
    projects.allocate(env.conn, actor=env.wh_a, project_id=pid, driver_id="DRV001", clock=env.clock)
    env.clock.advance(timedelta(days=1))
    client, tok = _client(env, tmp_path)
    client.cookies.set(sessions.COOKIE_NAME, tok["a"])
    r = client.post(f"/v1/projects/{pid}/allocate", json={"driver_id": "DRV002"})
    assert r.status_code == 409 and r.json()["reason_code"] == "PROJECT_ENDED"
    assert projects.drivers_of(env.conn, pid) == ["DRV001"]
    # Releasing from an ended project is still possible.
    projects.release(env.conn, actor=env.wh_a, project_id=pid, driver_id="DRV001", clock=env.clock)


def test_route_accepts_a_64_character_contract_ref(env: Env, tmp_path: Path) -> None:
    client, tok = _client(env, tmp_path)
    client.cookies.set(sessions.COOKIE_NAME, tok["a"])
    body = {"customer_name": "Acme", "contract_ref": "R" * 64, "facility_id": env.fac_a, "starts_on": "2026-08-01"}
    assert client.post("/v1/projects", json=body).status_code == 201


def _db_before_012(tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
    # A database migrated only up to 011, as an existing deployment would be.
    old = tmp_path / "old_migrations"
    old.mkdir()
    for sql in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if sql.name < "012":
            shutil.copy(sql, old / sql.name)
    path = tmp_path / "old.sqlite3"
    shutil.copyfile(load_settings().db_path, path)
    conn = open_connection(path=path)
    apply_migrations(conn, old)
    return path, conn


def _raw_project(conn: sqlite3.Connection, pid: str, ref: str) -> None:
    users.bootstrap_admin(conn, "boss@x.com", FakeClock(datetime.fromisoformat("2026-08-04T09:35:00+05:30")))
    boss = users.find_by_email(conn, "boss@x.com")
    assert boss is not None
    fac = conn.execute("select facility_id from facilities limit 1").fetchone()[0]
    conn.execute("insert into projects(project_id, customer_name, contract_ref, facility_id, status, created_by)"
                 " values (?,'A',?,?,'ACTIVE',?)", (pid, ref, fac, boss.user_id))
    conn.commit()


def test_migration_012_applies_over_existing_projects(tmp_path: Path) -> None:
    _, conn = _db_before_012(tmp_path)
    _raw_project(conn, "PRJ-1", "C-1")
    _raw_project(conn, "PRJ-2", "C-2")
    assert "012_project_contract_nocase.sql" in apply_migrations(conn)
    assert apply_migrations(conn) == []
    assert conn.execute("select count(*) from projects").fetchone()[0] == 2
    conn.close()


def test_migration_012_fails_loudly_on_case_duplicates(tmp_path: Path) -> None:
    path, conn = _db_before_012(tmp_path)
    _raw_project(conn, "PRJ-1", "Acme-1")
    _raw_project(conn, "PRJ-2", "ACME-1")
    with pytest.raises(sqlite3.IntegrityError, match="contract_ref_values_differ_only_by_case"):
        apply_migrations(conn)
    conn.close()
    # Nothing deleted, nothing stamped, no index.
    check = open_connection(path=path)
    try:
        assert check.execute("select count(*) from projects").fetchone()[0] == 2
        assert check.execute("select count(*) from schema_migrations where filename like '012%'").fetchone()[0] == 0
        assert check.execute("select count(*) from sqlite_master where name='ux_projects_contract_ref_nocase'").fetchone()[0] == 0
    finally:
        check.close()
