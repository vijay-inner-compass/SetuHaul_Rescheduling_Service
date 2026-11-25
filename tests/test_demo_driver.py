"""The local demo's separate driver account: it can send messages as its one driver and reach nothing else."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from setuhaul.db import open_connection
from setuhaul.demo_ui import build_demo_app
from setuhaul.stub_model import StubGateway
from tests.auth_helpers import build_world, demo_client

LOOPBACK = ("127.0.0.1", 50000)


def _app(tmp_path: Path) -> Any:
    # Built as scripts/run_demo.py builds it, plus a stand-in model for the tests.
    return build_demo_app(tmp_path, live_factory=StubGateway)


def _client(app: Any, **kwargs: Any) -> TestClient:
    options: dict[str, Any] = {"base_url": "http://localhost", "client": LOOPBACK, "follow_redirects": False}
    options.update(kwargs)
    return TestClient(app, **options)


def _driver(app: Any, driver_id: str = "DRV006") -> TestClient:
    client = _client(app)
    r = client.get(f"/demo/driver-login?driver={driver_id}")
    assert r.status_code == 302 and r.headers["location"] == "/demo/driver"
    return client


def test_driver_sends_a_message_and_the_console_sees_it(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    driver = _driver(app)
    r = driver.post("/demo/driver/api/message", json={"text": "I am running two hours late"})
    assert r.status_code == 200 and r.json()["http_status"] == 200
    mine = driver.get("/demo/driver/api/state").json()
    assert mine["driver"]["id"] == "DRV006" and mine["messages"][0]["text"] == "I am running two hours late"
    # The read-only console shows the same conversation.
    console = demo_client(app, base_url="http://localhost", client=LOOPBACK)
    assert console.get("/demo/api/driver/DRV006/state").json()["messages"] == mine["messages"]


def test_login_is_loopback_only_and_gives_no_cookie_otherwise(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    for client in (_client(app, client=("10.0.0.9", 50000)), _client(app, base_url="http://evil.example")):
        r = client.get("/demo/driver-login")
        assert r.status_code == 403 and not client.cookies


def test_unknown_driver_is_refused(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    assert _client(app).get("/demo/driver-login?driver=NOPE").status_code == 404


def test_driver_account_is_not_a_web_user_and_reaches_nothing_else(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    driver = _driver(app)
    for path in ("/demo", "/demo/api/board", "/demo/api/drivers", "/demo/api/driver/DRV007/state",
                 "/v1/warehouse/board?facility_id=FAC-JAI-01", "/v1/warehouse/driver-chats", "/v1/admin/audit"):
        assert driver.get(path).status_code == 401, path
    assert driver.post("/demo/api/chat", json={"driver_id": "DRV007", "text": "x"}).status_code == 401
    page = driver.get("/demo/driver")
    assert page.status_code == 200 and 'id="board"' not in page.text and "/demo/api/board" not in page.text


def test_the_driver_cannot_name_another_driver(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    r = _driver(app).post("/demo/driver/api/message", json={"text": "hi", "driver_id": "DRV007"})
    assert r.status_code == 422


@pytest.mark.parametrize("method,path", [("get", "/demo/driver"), ("get", "/demo/driver/api/state"), ("post", "/demo/driver/api/message")])
def test_anonymous_and_web_sessions_cannot_use_the_driver_routes(tmp_path: Path, method: str, path: str) -> None:
    app, _ = _app(tmp_path)
    kwargs: dict[str, Any] = {"json": {"text": "hi"}} if method == "post" else {}
    assert getattr(_client(app), method)(path, **kwargs).status_code == 401
    # A signed-in warehouse user is not a driver.
    console = demo_client(app, base_url="http://localhost", client=LOOPBACK)
    assert getattr(console, method)(path, **kwargs).status_code == 401


def test_a_suspended_driver_loses_the_account(tmp_path: Path) -> None:
    app, deps = _app(tmp_path)
    driver = _driver(app)
    conn = open_connection(path=deps.db_path)
    conn.execute("update drivers set driver_status='SUSPENDED' where driver_id='DRV006'")
    conn.commit()
    conn.close()
    assert driver.post("/demo/driver/api/message", json={"text": "hi"}).status_code == 401


def test_driver_routes_do_not_exist_outside_the_demo(tmp_path: Path, clock: Any) -> None:
    world = build_world(tmp_path, clock)
    from setuhaul.api import create_app

    client = _client(create_app(world.deps))
    for path in ("/demo/driver-login", "/demo/driver", "/demo/driver/api/state"):
        assert client.get(path).status_code == 404, path
