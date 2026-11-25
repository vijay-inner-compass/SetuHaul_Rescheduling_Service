"""The demo console's partner mailbox: request, email, reply, board."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from setuhaul.demo_ui import build_demo_app
from setuhaul.stub_model import StubGateway
from tests.auth_helpers import demo_client

PARTNER = "FAC-PARTNER-01"


@pytest.fixture()
def demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("SETUHAUL_ENV_FILE", "")
    monkeypatch.setenv("SETUHAUL_PARTNER_EMAIL_MODE", "simulated")
    app, deps = build_demo_app(tmp_path / "demo", live_factory=StubGateway)
    return demo_client(app), deps


def _board(client: TestClient) -> dict:  # type: ignore[type-arg]
    return client.get("/demo/api/board", params={"facility_id": PARTNER}).json()  # type: ignore[no-any-return]


def _booked(board: dict) -> list[dict]:  # type: ignore[type-arg]
    return [s for d in board["docks"] for s in d["slots"] if s["status"] == "BOOKED"]


def test_partner_facility_and_demo_load_are_seeded_and_labelled(demo) -> None:  # type: ignore[no-untyped-def]
    client, _ = demo
    cfg = client.get("/demo/api/config").json()
    assert PARTNER in [f["id"] for f in cfg["facilities"]]
    board = _board(client)
    assert len(_booked(board)) == 1 and board["pending_partner"] == []  # the load's original planner booking


def test_request_then_approve_shows_partner_booking_on_board(demo) -> None:  # type: ignore[no-untyped-def]
    client, _ = demo
    asked = client.post("/demo/api/partner-request", json={}).json()
    assert asked["status"] == "REQUESTED"
    box = client.get("/demo/api/partner-mailbox").json()
    assert box["mode"] == "simulated"
    assert box["requests"][0]["status"] == "PENDING_PARTNER" and box["requests"][0]["email_status"] == "SENT"
    assert asked["request_ref"] in box["sent"][0]["subject"] and "APPROVE" in box["sent"][0]["body"]
    assert len(_board(client)["pending_partner"]) == 1
    r = client.post("/demo/api/partner-reply", json={"request_ref": asked["request_ref"], "decision": "APPROVE"}).json()
    assert r["request_status"] == "APPROVED"
    board = _board(client)
    warehouse = [s for s in _booked(board) if s["partner"]]
    assert len(warehouse) == 1 and board["pending_partner"] == []
    assert client.get("/demo/api/partner-mailbox").json()["inbox"][0]["outcome"] == "BOOKED"


def test_decline_leaves_original_booking_on_board(demo) -> None:  # type: ignore[no-untyped-def]
    client, _ = demo
    ref = client.post("/demo/api/partner-request", json={}).json()["request_ref"]
    r = client.post("/demo/api/partner-reply", json={"request_ref": ref, "decision": "DECLINE"}).json()
    assert r["request_status"] == "DECLINED"
    board = _board(client)
    assert len(_booked(board)) == 1 and not any(s["partner"] for s in _booked(board))


def test_unanswered_request_expires_when_time_moves_on_and_sweeps(demo) -> None:  # type: ignore[no-untyped-def]
    client, _ = demo
    client.post("/demo/api/partner-request", json={})
    client.post("/demo/api/clock", json={"seconds": 7200, "sweep": True})
    client.post("/demo/api/clock", json={"seconds": 7200, "sweep": True})
    assert client.get("/demo/api/partner-mailbox").json()["requests"][0]["status"] == "EXPIRED"


def test_partner_reply_endpoint_is_rejected_in_live_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETUHAUL_ENV_FILE", "")
    monkeypatch.setenv("SETUHAUL_PARTNER_EMAIL_MODE", "live")
    for k in ("SMTP_USER", "SMTP_PASSWORD", "IMAP_USER", "IMAP_PASSWORD"):
        monkeypatch.setenv(f"SETUHAUL_{k}", "x")
    monkeypatch.setattr("setuhaul.demo_ui.build_mail", lambda s: (_Noop(), _Noop()))
    app, _ = build_demo_app(tmp_path / "d", live_factory=StubGateway)
    client = demo_client(app)
    r = client.post("/demo/api/partner-reply", json={"request_ref": "PR-AAAAAAAA", "decision": "APPROVE"})
    assert r.status_code == 409 and r.json()["reason_code"] == "REPLY_SIMULATION_IS_SIMULATED_MODE_ONLY"


class _Noop:
    def send(self, mail) -> None:  # type: ignore[no-untyped-def]
        return None

    def fetch_unseen(self) -> list:  # type: ignore[type-arg]
        return []

    def mark_seen(self, message_id: str) -> None:
        return None

    def close(self) -> None:
        return None


def test_partner_endpoints_reject_non_local_clients(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETUHAUL_ENV_FILE", "")
    app, _ = build_demo_app(tmp_path / "d", live_factory=StubGateway)
    outsider = demo_client(app, client=("203.0.113.9", 50000))
    assert outsider.get("/demo/api/partner-mailbox").status_code == 403
    bodies = {"partner-request": {}, "partner-reply": {"request_ref": "PR-AAAAAAAA", "decision": "APPROVE"}, "partner-sync": {}}
    for path, body in bodies.items():
        assert outsider.post(f"/demo/api/{path}", json=body).status_code == 403, path


def test_reset_clears_partner_requests_and_mailbox(demo) -> None:  # type: ignore[no-untyped-def]
    client, _ = demo
    client.post("/demo/api/partner-request", json={})
    client.post("/demo/api/reset")
    box = client.get("/demo/api/partner-mailbox").json()
    assert box["requests"] == [] and box["sent"] == [] and box["inbox"] == []


def test_bad_request_is_a_clear_answer_not_a_crash(demo) -> None:  # type: ignore[no-untyped-def]
    client, _ = demo
    assert client.post("/demo/api/partner-request", json={"shipment_id": "SHP1006"}).json()["status"] == "NOT_PARTNER_FACILITY"
    assert client.post("/demo/api/partner-request", json={"start": "25:99"}).status_code == 422
    assert client.post("/demo/api/partner-reply", json={"request_ref": "PR-NOPE", "decision": "APPROVE"}).status_code == 404


def test_sync_sends_a_request_queued_outside_the_console(demo) -> None:  # type: ignore[no-untyped-def]
    from setuhaul.db import open_connection
    from setuhaul.partner_booking import request_slot
    from tests.helpers import at

    client, deps = demo
    conn = open_connection(path=deps.demo.db_path)
    ref = request_slot(conn, deps.demo.clock, shipment_id="SHP-DEMO-P1", proposed_start=at("12:00"), proposed_end=at("13:00")).request_ref
    conn.close()
    assert client.get("/demo/api/partner-mailbox").json()["requests"][0]["email_status"] == "PENDING"
    assert client.post("/demo/api/partner-sync").json()["sent"] == 1
    box = client.get("/demo/api/partner-mailbox").json()
    assert box["requests"][0]["email_status"] == "SENT" and ref in box["sent"][0]["subject"]
