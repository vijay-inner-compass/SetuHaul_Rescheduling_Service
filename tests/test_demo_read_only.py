"""The driver console is read-only for web users: driver messages come from Telegram."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from setuhaul.db import open_connection
from setuhaul.demo_ui import build_demo_app
from setuhaul.stub_model import StubGateway
from tests.auth_helpers import add_user, demo_client, sign_in


def _app(tmp_path: Path) -> Any:
    # Built exactly as scripts/run_demo.py builds it: with a model, and no driver simulation.
    app, deps = build_demo_app(tmp_path, live_factory=StubGateway)
    return app, deps


def _count(deps: Any, sql: str) -> int:
    conn = open_connection(path=deps.db_path, readonly=True)
    try:
        return int(conn.execute(sql).fetchone()[0])
    finally:
        conn.close()


@pytest.mark.parametrize("role", ["ADMIN", "WAREHOUSE_AUTHORITY"])
@pytest.mark.parametrize("path,body", [("/demo/api/chat", {"driver_id": "DRV006", "text": "I will be late"}),
                                       ("/demo/api/gps", {"driver_id": "DRV006", "lat": 27.0, "lon": 75.0, "age_minutes": 0})])
def test_no_web_role_can_act_as_a_driver(tmp_path: Path, role: str, path: str, body: dict[str, Any]) -> None:
    app, deps = _app(tmp_path)
    client = demo_client(app, client=("127.0.0.1", 50000))
    if role == "ADMIN":
        conn = open_connection(path=deps.db_path)
        try:
            sign_in(client, conn, add_user(conn, deps.clock, "reader@x.com", "ADMIN"), deps.clock)
        finally:
            conn.close()
    before = (_count(deps, "select count(*) from chat_messages"), _count(deps, "select count(*) from gps_pings"))
    r = client.post(path, json=body)
    assert r.status_code == 403 and r.json()["reason_code"] == "READ_ONLY_CONSOLE"
    # Nothing was written as the driver, and the attempt is on the audit log.
    assert (_count(deps, "select count(*) from chat_messages"), _count(deps, "select count(*) from gps_pings")) == before
    assert _count(deps, "select count(*) from audit_log where action='DEMO_DRIVER_WRITE' and outcome='FORBIDDEN'") == 1


def test_reading_the_conversation_still_works_and_page_is_told(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    client = demo_client(app, client=("127.0.0.1", 50000))
    assert client.get("/demo/api/config").json()["read_only"] is True
    assert client.get("/demo/api/driver/DRV006/state").status_code == 200
    assert client.get("/demo/api/driver/DRV006/gps").status_code == 200
    page = client.get("/demo").text
    # The message box and ping form start hidden, so a slow config call never offers them.
    assert '<main class="readonly">' in page and ".readonly .composer" in page and ".readonly #gpsForm" in page


def test_run_demo_never_turns_simulation_on() -> None:
    script = (Path(__file__).resolve().parents[1] / "scripts" / "run_demo.py").read_text(encoding="utf-8")
    assert "driver_simulation" not in script
