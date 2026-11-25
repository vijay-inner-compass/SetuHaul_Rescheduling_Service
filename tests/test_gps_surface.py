"""The GPS discrepancy reaches the driver's reply, the search payload and the coordinator's case summary."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from setuhaul.clock import FakeClock
from setuhaul.demo_ui import build_demo_app
from setuhaul.gps import store_ping
from setuhaul.schemas import GpsPing
from setuhaul.stub_model import StubGateway  # a scripted stand-in model, for tests only
from tests.auth_helpers import demo_client
from tests.conv_helpers import Chat, say_text, tc, tool_json
from tests.helpers import seed_everything_known

# What the driver reports: 5 minutes late (far below what a Delhi ping implies).
LATE = {"kind": "late_by", "minutes": 5}
# Worked by hand in test_gps_tools: Delhi ping vs Jaipur dock gives a 406 minute gap against a 5 minute delay report.
DELHI_GAP = 406


@pytest.fixture()
def chat(db: sqlite3.Connection, clock: FakeClock, tmp_path: Path) -> Chat:
    # A conversation with the shared seed data loaded.
    seed_everything_known(db)
    return Chat(db, clock, tmp_path)


def _delhi_ping(chat: Chat) -> None:
    # A fresh ping near Delhi, far from the Jaipur dock.
    store_ping(chat.db, "SHP1006", GpsPing(lat=28.6, lon=77.2, recorded_at=chat.clock.now() - timedelta(minutes=1)), chat.clock, "TEST")


def _search(chat: Chat, **extra: Any) -> tuple[dict[str, Any], Any]:
    # Drive one search_slot_options call and return (the payload the model saw, the reply).
    seen: dict[str, Any] = {}

    def grab(msgs):  # type: ignore[no-untyped-def]
        seen.update(tool_json(msgs, "search_slot_options"))
        return say_text("ok")

    reply = chat.say("SHP1006 is running late", [tc("search_slot_options", shipment_id="SHP1006", report=LATE, **extra), grab])
    return seen, reply


def test_search_payload_and_reply_carry_the_gap(chat: Chat) -> None:
    _delhi_ping(chat)
    payload, reply = _search(chat)
    # The OK payload gains the gap, the source and the lowered confidence.
    assert payload["status"] == "OK"
    assert payload["gps_discrepancy_minutes"] == DELHI_GAP
    assert payload["eta_source"] == "DRIVER_DECLARED" and payload["confidence"] == "LOW"
    # The driver's reply (worded by the system from the menu) ends with the GPS note carrying the same number.
    assert f"differs from your report by about {DELHI_GAP} minutes" in reply.text


def test_handoff_summary_carries_the_gap(chat: Chat) -> None:
    _delhi_ping(chat)
    # An impossible window (latest start 09:40, now 09:35) leaves no slot, so a case is opened for the coordinator.
    _, reply = _search(chat, latest_time="09:40")
    assert reply.status == "HANDOFF" and reply.case_id
    req = chat.sink.requests[-1]
    assert f"GPS disagrees with the driver's ETA by {DELHI_GAP} min" in req.summary
    assert req.context["gps_discrepancy_minutes"] == DELHI_GAP and req.context["eta_source"] == "DRIVER_DECLARED"


def test_without_a_ping_nothing_changes(chat: Chat) -> None:
    payload, reply = _search(chat)
    # No GPS evidence: today's payload keys and a reply without any GPS note.
    assert payload["status"] == "OK"
    assert set(payload) == {"status", "option_set_id", "held", "options", "valid_until"}
    assert "GPS" not in reply.text
    # And the no-slot hand-off summary is today's plain sentence.
    _search(chat, latest_time="09:40")
    req = chat.sink.requests[-1]
    assert "GPS" not in req.summary and "gps_discrepancy_minutes" not in req.context


def test_demo_far_ping_then_chat_shows_the_note(tmp_path: Path) -> None:
    # Through the demo routes: post a ping 120 km out, then tell the driver's 20 minute lie to the real pipeline.
    client = demo_client(build_demo_app(tmp_path / "demo", live_factory=StubGateway, driver_simulation=True)[0])
    did = next(d["id"] for d in client.get("/demo/api/drivers").json()["drivers"] if d["name"] == "Manoj Sharma")
    fac = client.get(f"/demo/api/driver/{did}/gps").json()["facility"]
    sent = client.post("/demo/api/gps", json={"driver_id": did, "lat": fac["lat"] + 120 / 111.2, "lon": fac["lon"], "age_minutes": 0})
    assert sent.status_code == 200
    reply = client.post("/demo/api/chat", json={"driver_id": did, "text": "my truck is 20 minutes late"}).json()["reply"]
    # Planned 10:20 + 20 = 10:40; GPS ETA 09:35:20 + 234 min = 13:29:20; the gap is 169 min 20 s, reported as 169.
    assert "differs from your report by about 169 minutes" in reply
    # (The demo seeds a fresh fixture ping for every driver, so "no ping" is covered by the Chat tests above.)


# The GPS note makes no claim about who else can see it: menu turns open no case, so nothing is visible to operations.
def test_gps_note_makes_no_operations_claim_in_any_language() -> None:
    from setuhaul.language import say

    for lang in ("en", "hi", "hinglish"):
        text = say("gps_note", lang, minutes="42")
        assert "42" in text and text.rstrip()[-1] in ".।"
        assert not any(w in text for w in ("Operations", "ऑपरेशंस", "can see", "dekh"))


# Two searches in one turn: a gap found by the first must not be attached to the second search's menu.
def test_discrepancy_does_not_leak_into_a_second_search_in_the_same_turn(chat: Chat) -> None:
    _delhi_ping(chat)
    seen: list[dict[str, Any]] = []

    def second(msgs):  # type: ignore[no-untyped-def]
        # After the first search, make GPS agree with the report (a ping 30 km out is about 59 min away, near the 10:25 ETA).
        seen.append(tool_json(msgs, "search_slot_options"))
        store_ping(chat.db, "SHP1006", GpsPing(lat=26.9124 + 30 / 111.2, lon=75.7873, recorded_at=chat.clock.now()), chat.clock, "TEST")
        return tc("search_slot_options", call_id="t2", shipment_id="SHP1006", report={"kind": "late_by", "minutes": 5})

    def finish(msgs):  # type: ignore[no-untyped-def]
        seen.append(tool_json(msgs, "search_slot_options"))
        return say_text("ok")

    reply = chat.say("SHP1006 is running late", [tc("search_slot_options", shipment_id="SHP1006", report=LATE), second, finish])
    # The first payload had the gap, the second did not.
    assert seen[0]["gps_discrepancy_minutes"] == DELHI_GAP and "gps_discrepancy_minutes" not in seen[1]
    # So the rendered menu carries no GPS note.
    assert "GPS" not in reply.text
