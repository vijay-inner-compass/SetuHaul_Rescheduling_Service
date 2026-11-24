"""How a partner request and a partner booking look to drivers and on the dock board."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from setuhaul.demo_ui import build_demo_app
from setuhaul.language import LANGS
from setuhaul.partner_booking import handle_reply, render_partner_status, request_slot
from setuhaul.stub_model import StubGateway
from tests.helpers import at, seed_partner_facility

CONTACT = "partner.desk@partner.example"


def _approve(conn: sqlite3.Connection, clock: FakeClock, ref: str, mid: str = "<ok@p>") -> None:
    out = handle_reply(conn, clock, message_id=mid, from_addr=CONTACT, subject=f"Re: [{ref}]", in_reply_to=None,
                       body="APPROVE", authentication_results=None)
    assert out.status == "BOOKED"


@pytest.fixture()
def demo(tmp_path: Path):  # type: ignore[no-untyped-def]
    app, deps = build_demo_app(tmp_path / "demo", live_factory=StubGateway)
    conn = open_connection(path=deps.demo.db_path)
    seed_partner_facility(conn, "SHP1006")
    yield TestClient(app), deps, conn
    conn.close()


def _ask(conn: sqlite3.Connection, clock: FakeClock) -> str:
    ref = request_slot(conn, clock, shipment_id="SHP1006", proposed_start=at("14:00"), proposed_end=at("15:00")).request_ref
    assert ref
    return ref


def test_board_marks_warehouse_source_slot_as_partner(demo) -> None:  # type: ignore[no-untyped-def]
    client, deps, conn = demo
    _approve(conn, deps.demo.clock, _ask(conn, deps.demo.clock))
    board = client.get("/demo/api/board", params={"facility_id": "FAC-PARTNER-01"}).json()
    booked = [s for d in board["docks"] for s in d["slots"] if s["status"] == "BOOKED"]
    assert len(booked) == 1 and booked[0]["partner"] is True and booked[0]["source"] == "WAREHOUSE"
    jaipur = client.get("/demo/api/board").json()
    assert not any(s["partner"] for d in jaipur["docks"] for s in d["slots"])


def test_board_lists_pending_request_separately_not_as_booked(demo) -> None:  # type: ignore[no-untyped-def]
    client, deps, conn = demo
    ref = _ask(conn, deps.demo.clock)
    board = client.get("/demo/api/board", params={"facility_id": "FAC-PARTNER-01"}).json()
    assert not any(s["status"] == "BOOKED" for d in board["docks"] for s in d["slots"])
    assert [p["request_ref"] for p in board["pending_partner"]] == [ref]
    assert board["pending_partner"][0]["order_reference"] == "ORD-260804-006"


def test_pending_wording_never_says_confirmed(db: sqlite3.Connection, clock: FakeClock) -> None:
    seed_partner_facility(db)
    request_slot(db, clock, shipment_id="SHP1006", proposed_start=at("14:00"), proposed_end=at("15:00"))
    for lang in LANGS:
        text = render_partner_status(db, "SHP1006", lang) or ""
        assert text
        assert "confirmed" not in text.lower() and "booked" not in text.lower()
    assert "waiting" in (render_partner_status(db, "SHP1006", "en") or "")


def test_declined_wording_says_original_unchanged(db: sqlite3.Connection, clock: FakeClock) -> None:
    seed_partner_facility(db)
    ref = request_slot(db, clock, shipment_id="SHP1006", proposed_start=at("14:00"), proposed_end=at("15:00")).request_ref
    handle_reply(db, clock, message_id="<d>", from_addr=CONTACT, subject=f"[{ref}]", in_reply_to=None, body="DECLINE",
                 authentication_results=None)
    assert "unchanged" in (render_partner_status(db, "SHP1006", "en") or "")


def test_approved_wording_states_time_and_no_request_gives_none(db: sqlite3.Connection, clock: FakeClock) -> None:
    assert render_partner_status(db, "SHP1006") is None
    seed_partner_facility(db)
    ref = request_slot(db, clock, shipment_id="SHP1006", proposed_start=at("14:00"), proposed_end=at("15:00")).request_ref
    _approve(db, clock, ref or "")
    assert "14:00" in (render_partner_status(db, "SHP1006", "en") or "")


def test_driver_is_told_in_chat_when_partner_approves(db: sqlite3.Connection, clock: FakeClock) -> None:
    seed_partner_facility(db)
    ref = request_slot(db, clock, shipment_id="SHP1006", proposed_start=at("14:00"), proposed_end=at("15:00")).request_ref
    sql = ("select message_text from chat_messages m join chat_threads t using(thread_id) "
           "where t.driver_id='DRV006' and m.sender_type='OPERATIONS'")
    before = len(db.execute(sql).fetchall())
    _approve(db, clock, ref or "")
    rows = db.execute(sql).fetchall()
    assert len(rows) == before + 1
    assert any("14:00" in r["message_text"] and "approved" in r["message_text"] for r in rows)
