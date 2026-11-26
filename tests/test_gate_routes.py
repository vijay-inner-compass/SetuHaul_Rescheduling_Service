"""The gate routes and screens."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from setuhaul.api import create_app
from setuhaul.db import open_connection
from setuhaul.dock_pass import find, normalise
from tests.auth_helpers import sign_in
from tests.gate_helpers import Yard


class Web:
    def __init__(self, tmp_path: Path) -> None:
        self.yard = Yard(tmp_path)
        self.app = create_app(replace(self.yard.world.deps, redis=self.yard.redis))

    def client(self, who: str | None = "guard") -> TestClient:
        client = TestClient(self.app, base_url="http://localhost", follow_redirects=False)
        if who:
            conn = open_connection(path=self.yard.world.path)
            sign_in(client, conn, self.yard.world.users[who], self.yard.world.clock)
            conn.close()
        return client

    def body(self, **over: str) -> dict[str, str]:
        return {"code": self.yard.code, "vehicle_registration": self.yard.registration, "licence_number": self.yard.licence,
                "request_key": "k1"} | over


@pytest.fixture()
def web(tmp_path: Path) -> Web:
    return Web(tmp_path)


def test_roles(web: Web) -> None:
    for path in ("/v1/gate/validate", "/v1/gate/check-in", "/v1/gate/check-out"):
        assert web.client(None).post(path, json={}).status_code == 401
        assert web.client("wh_a").post(path, json=web.body()).status_code == 403
    assert web.client("wh_a").get("/gate").status_code == 403
    assert web.client().get("/warehouse").status_code == 403
    assert web.client("guard").post("/v1/warehouse/visits/TV-X/unloading-complete", json={}).status_code == 403
    assert web.client("admin").get("/gate").status_code == 200


def test_the_visit_through_the_routes_never_returns_the_code(web: Web) -> None:
    guard, seen = web.client(), []

    def call(client: TestClient, path: str, body: dict[str, str], status: int = 200) -> dict[str, str]:
        response = client.post(path, json=body)
        seen.append(response.text)
        assert response.status_code == status, response.text
        return response.json()

    visit = call(guard, "/v1/gate/validate", web.body())["truck_visit_id"]
    call(guard, "/v1/gate/check-in", web.body())
    again = call(guard, "/v1/gate/check-in", web.body(request_key="k2"), 409)
    assert again["reason_code"] == "ALREADY_CHECKED_IN" and "already" in again["message"].lower()
    seen.append(guard.get("/v1/gate/visits").text)
    page = guard.get("/gate").text
    assert visit in seen[-1] and web.yard.registration in page and "Check out" in page
    early = call(guard, "/v1/gate/check-out", {"truck_visit_id": visit, "request_key": "o1"}, 409)
    assert early["reason_code"] == "UNLOADING_NOT_COMPLETE" and early["message"]
    warehouse = web.client("wh_a")
    assert "Unloading complete" in warehouse.get("/warehouse").text
    call(warehouse, f"/v1/warehouse/visits/{visit}/unloading-complete", {})
    call(guard, "/v1/gate/check-out", {"code": web.yard.code, "request_key": "o1"})
    call(guard, "/v1/gate/validate", web.body(), 422)
    secret = normalise(web.yard.code)[3:]
    assert not any(find(text) or secret in normalise(text) for text in [*seen, page, warehouse.get("/warehouse").text])


def test_statuses_and_messages(web: Web) -> None:
    guard = web.client()
    unknown = guard.post("/v1/gate/validate", json=web.body(code="DAP-AAAAA-AAAAA-AAAAA-AAAAA-AAAAAA"))
    assert unknown.status_code == 422 and unknown.json()["reason_code"] == "PASS_NOT_VALID" and "warehouse" in unknown.json()["message"]
    assert guard.post("/v1/gate/check-out", json={"truck_visit_id": "TV-NOPE", "request_key": "x"}).status_code == 404
    bad = guard.post("/v1/gate/validate", json=web.body() | {"extra": "x"})
    assert bad.status_code == 422 and web.yard.code not in bad.text
    for _ in range(10):
        guard.post("/v1/gate/validate", json=web.body(licence_number="WRONG"))
    assert guard.post("/v1/gate/validate", json=web.body()).status_code == 429
    other = guard.post("/v1/gate/validate", json=web.body(), headers={"origin": "https://evil.example"})
    assert other.status_code == 403


def test_the_screen(web: Web) -> None:
    page = web.client().get("/gate").text
    assert 'id="gate-form"' in page and page.count('autocomplete="off"') >= 4
    assert 'id="check-in" hidden' in page and "No trucks are inside." in page
    script = web.client().get("/app/assets/gate.js")
    assert script.status_code == 200 and "randomUUID" in script.text and "innerHTML" not in script.text
