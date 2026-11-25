"""Test helpers for the auth work: users, session cookies and a small world with two facilities of data."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import fakeredis
from fastapi import FastAPI
from fastapi.testclient import TestClient

from setuhaul.api import AppDeps, create_app
from setuhaul.auth import projects, sessions, users
from setuhaul.auth.users import Role, User
from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection

# The two supplied facilities and the loads and drivers headed for each.
JAIPUR, GURUGRAM = "FAC-JAI-01", "FAC-GGN-01"


def add_user(conn: sqlite3.Connection, clock: FakeClock, email: str, role: Role, facilities: tuple[str, ...] = ()) -> User:
    # Direct insert, so a warehouse authority with no facility can exist (the invite flow refuses that on purpose).
    uid = "USR-" + email.split("@")[0].upper()
    conn.execute("insert into users(user_id, email, role, status, display_name, invited_by, created_at) values (?,?,?,'ACTIVE',?,NULL,?)",
                 (uid, email, role, email.split("@")[0].title(), clock.now().isoformat()))
    for fac in facilities:
        conn.execute("insert into user_facilities(user_id, facility_id) values (?,?)", (uid, fac))
    conn.commit()
    found = users.find_by_id(conn, uid)
    assert found is not None
    return found


def sign_in(client: TestClient, conn: sqlite3.Connection, user: User, clock: FakeClock) -> None:
    # A real server-side session, carried by the cookie the app reads.
    client.cookies.set(sessions.COOKIE_NAME, sessions.create(conn, user, "GOOGLE", clock))


def sign_in_demo(client: TestClient, deps: AppDeps) -> None:
    # The demo app seeds its own warehouse authority; give the client a session for it.
    conn = open_connection(path=deps.db_path)
    try:
        user = users.find_by_email(conn, "demo@localhost.test")
        assert user is not None
        sign_in(client, conn, user, deps.clock)  # type: ignore[arg-type]
    finally:
        conn.close()


@dataclass
class World:
    path: Path
    clock: FakeClock
    deps: AppDeps
    app: FastAPI
    # Users by short name: admin, wh_a (Jaipur), wh_b (Gurugram), wh_none (no facility).
    users: dict[str, User]
    # Cases by facility.
    cases: dict[str, str]

    def client(self, who: str | None = None) -> TestClient:
        # A fresh client per caller, so cookies never leak between roles.
        client = TestClient(self.app, base_url="http://localhost", follow_redirects=False)
        if who is not None:
            conn = open_connection(path=self.path)
            try:
                sign_in(client, conn, self.users[who], self.clock)
            finally:
                conn.close()
        return client


def build_world(tmp_path: Path, clock: FakeClock) -> World:
    path = make_working_db(tmp_path / "w.sqlite3")
    conn = open_connection(path=path)
    boss = add_user(conn, clock, "boss@x.com", "ADMIN")
    wh_a = add_user(conn, clock, "wha@x.com", "WAREHOUSE_AUTHORITY", (JAIPUR,))
    wh_b = add_user(conn, clock, "whb@x.com", "WAREHOUSE_AUTHORITY", (GURUGRAM,))
    wh_none = add_user(conn, clock, "whnone@x.com", "WAREHOUSE_AUTHORITY", ())
    # A project per warehouse authority, each with the driver of one load at their facility.
    for owner, fac, driver, n in ((wh_a, JAIPUR, "DRV001", 1), (wh_b, GURUGRAM, "DRV006", 2)):
        pid = projects.create(conn, actor=owner, customer_name="Acme", contract_ref=f"C-{n}", facility_id=fac,
                              starts_on=clock.now().date(), ends_on=None, clock=clock)
        projects.allocate(conn, actor=owner, project_id=pid, driver_id=driver, clock=clock)
    # One case per facility, tied to a load whose destination is that facility; plus one with no load (admin only).
    cases: dict[str, str] = {}
    stamp = clock.now().isoformat()
    for fac, shipment, driver in ((JAIPUR, "SHP1001", "DRV001"), (GURUGRAM, "SHP1021", "DRV006"), ("NONE", None, "DRV001")):
        thread = f"THR-T-{fac}"
        conn.execute("insert into chat_threads(thread_id, driver_id, shipment_id, opened_at, thread_status, thread_intent) "
                     "values (?,?,?,?,'OPEN','UNKNOWN')", (thread, driver, shipment, stamp))
        conn.execute("insert into chat_messages(chat_message_id, thread_id, sender_type, message_text, message_ts) "
                     "values (?,?,'DRIVER',?,?)", (f"MSG-T-{fac}", thread, f"hello from {driver} for {fac}", stamp))
        case_id = f"ESC-T-{fac}"
        conn.execute("insert into escalation_cases(case_id, dedupe_key, status, reason_code, driver_id, thread_id, shipment_id, summary,"
                     " created_at, updated_at) values (?,?,'OPEN','NO_SLOT',?,?,?,?,?,?)",
                     (case_id, case_id, driver, thread, shipment, "needs help", stamp, stamp))
        cases[fac] = case_id
    # One partner slot request per facility (each needs a contact at that facility).
    for fac, shipment in ((JAIPUR, "SHP1001"), (GURUGRAM, "SHP1021")):
        conn.execute("insert into facility_contacts(contact_id, facility_id, contact_role, contact_name, email, active_flag) "
                     "values (?,?,'WAREHOUSE_COORDINATOR','Desk','desk@x.example',1)", (f"CON-T-{fac}", fac))
        conn.execute("insert into partner_requests(request_ref, shipment_id, facility_id, contact_id, proposed_start, proposed_end, status,"
                     " created_at, expires_at) values (?,?,?,?,?,?,'PENDING_PARTNER',?,?)",
                     (f"PR-T-{fac}", shipment, fac, f"CON-T-{fac}", stamp, stamp, stamp, stamp))
    conn.commit()
    conn.close()
    deps = AppDeps(db_path=path, redis=fakeredis.FakeRedis(decode_responses=True), webhook_secret=b"w" * 16,
                   coordinator_tokens={"tok-asha": "Asha Verma"}, fallback_path=tmp_path / "f.jsonl", clock=clock,
                   session_secret=b"s" * 32)
    return World(path=path, clock=clock, deps=deps, app=create_app(deps),
                 users={"admin": boss, "wh_a": wh_a, "wh_b": wh_b, "wh_none": wh_none}, cases=cases)


def demo_client(app: FastAPI, **kwargs: object) -> TestClient:
    # A client already signed in as the demo console's seeded user (the console needs a web session now).
    client = TestClient(app, **kwargs)  # type: ignore[arg-type]
    sign_in_demo(client, app.state.deps)
    return client
