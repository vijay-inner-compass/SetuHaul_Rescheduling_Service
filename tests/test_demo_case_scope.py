"""The demo console's case routes follow the same facility scope as the coordinator API."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from setuhaul.db import open_connection
from setuhaul.demo_ui import build_demo_app
from setuhaul.escalation import HandoffRequest, create_handoff, get_case
from setuhaul.stub_model import StubGateway
from tests.auth_helpers import JAIPUR, add_user, demo_client, sign_in

# A load bound for each supplied facility, and one hand-off that names no load.
LOADS = {"mine": ("SHP1001", "DRV001"), "other": ("SHP1021", "DRV006"), "no_load": (None, "DRV001")}


@pytest.fixture()
def console(tmp_path: Path) -> Any:
    app, deps = build_demo_app(tmp_path, live_factory=StubGateway)
    conn = open_connection(path=deps.db_path)
    try:
        cases = {}
        for name, (shipment, driver) in LOADS.items():
            thread = f"THR-T-{name}"
            conn.execute("insert into chat_threads(thread_id, driver_id, shipment_id, opened_at, thread_status, thread_intent) "
                         "values (?,?,?,?,'OPEN','UNKNOWN')", (thread, driver, shipment, deps.clock.now().isoformat()))
            conn.commit()
            cases[name] = create_handoff(conn, HandoffRequest(reason_code="NO_SLOT", driver_id=driver, thread_id=thread,
                                                              shipment_id=shipment, created_at=deps.clock.now()), deps.clock).case_id
        users = {"wh": add_user(conn, deps.clock, "wha@x.com", "WAREHOUSE_AUTHORITY", (JAIPUR,)),
                 "admin": add_user(conn, deps.clock, "boss@x.com", "ADMIN")}
        clients = {}
        for name, user in users.items():
            clients[name] = demo_client(app, client=("127.0.0.1", 50000))
            clients[name].cookies.clear()
            sign_in(clients[name], conn, user, deps.clock)
    finally:
        conn.close()
    return deps, clients, cases


def _status(deps: Any, case_id: str) -> str:
    conn = open_connection(path=deps.db_path, readonly=True)
    try:
        case = get_case(conn, case_id)
        assert case is not None
        return case.status
    finally:
        conn.close()


def _listed(client: Any) -> set[str]:
    return {c["case_id"] for c in client.get("/demo/api/cases").json()["cases"]}


def test_warehouse_lists_only_its_facility_cases_and_admin_lists_all(console: Any) -> None:
    _, clients, cases = console
    assert _listed(clients["wh"]) == {cases["mine"]}
    assert _listed(clients["admin"]) == set(cases.values())


@pytest.mark.parametrize("which", ["other", "no_load"])
def test_warehouse_cannot_claim_or_resolve_a_case_outside_its_facility(console: Any, which: str) -> None:
    deps, clients, cases = console
    cid = cases[which]
    r = clients["wh"].post(f"/demo/api/cases/{cid}/claim", json={"coordinator": "Asha Verma"})
    assert r.status_code == 404 and r.json()["status"] == "NOT_FOUND"
    assert _status(deps, cid) == "OPEN"
    # Claimed by someone allowed to: the outsider still cannot close it, even under the claimer's name.
    assert clients["admin"].post(f"/demo/api/cases/{cid}/claim", json={"coordinator": "Asha Verma"}).status_code == 200
    r = clients["wh"].post(f"/demo/api/cases/{cid}/resolve",
                           json={"coordinator": "Asha Verma", "resolution_code": "ADVISED_DRIVER", "note": "x"})
    assert r.status_code == 404 and r.json()["status"] == "NOT_FOUND"
    assert _status(deps, cid) == "CLAIMED"


def test_warehouse_can_still_work_its_own_case(console: Any) -> None:
    deps, clients, cases = console
    cid = cases["mine"]
    assert clients["wh"].post(f"/demo/api/cases/{cid}/claim", json={"coordinator": "Asha Verma"}).status_code == 200
    r = clients["wh"].post(f"/demo/api/cases/{cid}/resolve",
                           json={"coordinator": "Asha Verma", "resolution_code": "ADVISED_DRIVER", "note": "done"})
    assert r.status_code == 200
    assert _status(deps, cid) == "RESOLVED"
