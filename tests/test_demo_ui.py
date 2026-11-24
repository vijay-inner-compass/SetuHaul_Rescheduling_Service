"""The demonstration console: people not ids, the real flow underneath, local only."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from setuhaul.demo_ui import build_demo_app
from setuhaul.stub_model import StubGateway  # a scripted stand-in model, for tests only


@pytest.fixture()
def demo(tmp_path: Path):  # type: ignore[no-untyped-def]
    app, deps = build_demo_app(tmp_path / "demo", live_factory=StubGateway)
    return TestClient(app), deps


def driver_id(client: TestClient, name: str) -> str:
    return next(d["id"] for d in client.get("/demo/api/drivers").json()["drivers"] if d["name"] == name)


def say(client: TestClient, did: str, text: str) -> dict:  # type: ignore[type-arg]
    r = client.post("/demo/api/chat", json={"driver_id": did, "text": text})
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


def cell(client: TestClient, label_part: str) -> dict | None:  # type: ignore[type-arg]
    board = client.get("/demo/api/board").json()
    return next((s for d in board["docks"] for s in d["slots"] if label_part in (s["label"] or "") and s["status"] in ("HELD", "BOOKED")), None)


# --- the page ---------------------------------------------------------------------------------------------
def test_page_is_served_with_the_three_panels(demo) -> None:  # type: ignore[no-untyped-def]
    client, _ = demo
    html = client.get("/demo").text
    for needle in ("Driver console", "Dock board", "Operations", "SetuHaul Dock Scheduling", "Demonstration"):
        assert needle in html
    assert "<script src=" not in html and "https://" not in html.replace("http://www.w3.org", "")  # nothing loaded from outside


def test_the_page_escapes_everything_it_renders() -> None:
    html = (Path(__file__).resolve().parents[1] / "src" / "setuhaul" / "demo" / "index.html").read_text(encoding="utf-8")
    assert "const esc=" in html and html.count("esc(") > 40
    assert "innerHTML=" in html and "textContent" in html


def test_demo_routes_refuse_anyone_but_this_machine(tmp_path: Path) -> None:
    app, _ = build_demo_app(tmp_path / "d", live_factory=StubGateway)
    outsider = TestClient(app, client=("203.0.113.9", 50000))
    for path in ("/demo", "/demo/api/drivers", "/demo/api/board", "/demo/api/cases", "/demo/api/config"):
        r = outsider.get(path)
        assert r.status_code == 403 and r.json()["reason_code"] == "DEMO_IS_LOCAL_ONLY", path
    assert outsider.post("/demo/api/reset").status_code == 403
    assert outsider.post("/demo/api/chat", json={"driver_id": "x", "text": "hi"}).status_code == 403


def test_demo_routes_do_not_exist_when_the_demo_is_off(tmp_path: Path) -> None:
    import fakeredis

    from setuhaul.api import AppDeps, create_app
    from setuhaul.db import make_working_db

    deps = AppDeps(db_path=make_working_db(tmp_path / "x.sqlite3"), redis=fakeredis.FakeRedis(decode_responses=True), webhook_secret=b"s",
                   coordinator_tokens={}, fallback_path=tmp_path / "f.jsonl")
    assert TestClient(create_app(deps)).get("/demo").status_code == 404


# --- names, not ids ---------------------------------------------------------------------------------------
def test_drivers_are_listed_by_name_with_loads_by_order_and_customer(demo) -> None:  # type: ignore[no-untyped-def]
    client, _ = demo
    drivers = client.get("/demo/api/drivers").json()["drivers"]
    names = {d["name"] for d in drivers}
    assert {"Manoj Sharma", "Arjun Das", "Pradeep Jat"} <= names and len(drivers) == 16  # 15 supplied + the seeded partner-warehouse demo driver
    manoj = next(d for d in drivers if d["name"] == "Manoj Sharma")
    assert {s["order"] for s in manoj["shipments"]} == {"ORD-260804-006", "ORD-260804-021"}
    assert manoj["carrier"] == "Shakti Transport Services" and manoj["shipments"][0]["customer"]
    shown = json.dumps([{k: v for k, v in d.items() if k != "id"} for d in drivers])
    assert not re.search(r"DRV\d{3}|SHP\d{4}", shown)  # the only place an id appears is the opaque selector value


def test_the_board_labels_bookings_with_people_and_orders(demo) -> None:  # type: ignore[no-untyped-def]
    client, _ = demo
    board = client.get("/demo/api/board").json()
    labels = [s["label"] for d in board["docks"] for s in d["slots"] if s["status"] == "BOOKED"]
    assert labels and all("·" in x and not re.search(r"DRV\d|SHP\d", x) for x in labels)
    assert any("Manoj Sharma" in x for x in labels)


# --- the real flow through the page's endpoints ------------------------------------------------------------
def test_chat_hold_confirm_updates_transcript_board_and_history(demo) -> None:  # type: ignore[no-untyped-def]
    client, _ = demo
    manoj = driver_id(client, "Manoj Sharma")
    first = say(client, manoj, "my truck is 60 minutes late")
    assert first["status"] == "REPLIED" and first["reply"].startswith("These slots are available. Nothing is held yet:\n1. Dock ") and first["http_status"] == 200
    held = say(client, manoj, "I will take the first one")
    assert "holding" in held["reply"]
    state = client.get(f"/demo/api/driver/{manoj}/state").json()
    assert [m["who"] for m in state["messages"]] == ["driver", "assistant", "driver", "assistant"]
    assert state["hold"] and "dock" in state["hold"]["slot"] and state["hold"]["seconds_left"] > 0
    slot = cell(client, "Manoj Sharma")
    assert slot is not None and slot["status"] in ("HELD", "BOOKED")
    done = say(client, manoj, "yes confirm")
    assert done["reply"].startswith("Your appointment is confirmed")
    after = client.get(f"/demo/api/driver/{manoj}/state").json()
    assert after["hold"] is None
    events = client.get("/demo/api/activity").json()
    texts = " ".join(e["text"] for e in events["events"])
    assert "appointment confirmed" in texts and "original appointment" in texts
    assert {o["role"] for o in events["outbox"]} >= {"Warehouse Planner", "Consignee", "Operations"}
    assert all("Manoj Sharma" in e["who"] for e in events["events"] if e["who"])


def test_each_message_moves_the_clock_and_the_buttons_do_too(demo) -> None:  # type: ignore[no-untyped-def]
    client, _deps = demo
    start = client.get("/demo/api/config").json()["clock"]["iso"]
    say(client, driver_id(client, "Manoj Sharma"), "hello there")
    after = client.get("/demo/api/config").json()["clock"]["iso"]
    assert after > start
    r = client.post("/demo/api/clock", json={"seconds": 60}).json()
    assert r["swept"] == 0 and r["clock"]["iso"] > after


def test_a_hold_lapses_when_time_moves_on_and_the_driver_is_told(demo) -> None:  # type: ignore[no-untyped-def]
    client, _ = demo
    manoj = driver_id(client, "Manoj Sharma")
    say(client, manoj, "my truck is 60 minutes late")
    say(client, manoj, "I will take the first one")
    assert client.get(f"/demo/api/driver/{manoj}/state").json()["hold"]
    moved = client.post("/demo/api/clock", json={"seconds": 181, "sweep": True}).json()
    assert moved["swept"] == 1
    assert client.get(f"/demo/api/driver/{manoj}/state").json()["hold"] is None
    outbox = client.get("/demo/api/activity").json()["outbox"]
    assert any(o["role"] == "Driver" and o["status"] == "PENDING" for o in outbox)
    assert client.post("/demo/api/deliver").json()["sent"] >= 1
    assert all(o["status"] == "SENT" for o in client.get("/demo/api/activity").json()["outbox"])


def test_planner_booking_takes_a_slot_and_a_second_attempt_conflicts(demo) -> None:  # type: ignore[no-untyped-def]
    client, _ = demo
    free = next(s for d in client.get("/demo/api/board").json()["docks"] for s in d["slots"] if s["status"] == "AVAILABLE" and not s["past"])
    ok = client.post("/demo/api/planner-booking", json={"slot_id": free["slot_id"]})
    assert ok.status_code == 200 and "·" in ok.json()["for"]
    again = client.post("/demo/api/planner-booking", json={"slot_id": free["slot_id"]})
    assert again.status_code == 409 and again.json()["reason_code"] == "SLOT_NOT_FREE"
    board = client.get("/demo/api/board").json()
    assert next(s for d in board["docks"] for s in d["slots"] if s["slot_id"] == free["slot_id"])["status"] == "BOOKED"


def test_a_driver_who_cannot_be_served_becomes_a_case_a_coordinator_can_work(demo) -> None:  # type: ignore[no-untyped-def]
    client, _ = demo
    pradeep = driver_id(client, "Pradeep Jat")  # 21,000 kg, 75 minute unload: no single slot fits
    r = say(client, pradeep, "my truck is 70 minutes late")
    assert r["status"] == "HANDOFF" and r["reason_code"] == "NO_SLOT" and r["case_id"]
    cases = client.get("/demo/api/cases").json()["cases"]
    assert len(cases) == 1 and cases[0]["driver"] == "Pradeep Jat" and cases[0]["status"] == "OPEN" and "ORD-" in cases[0]["load"]
    assert cases[0]["reason"] == "No slot fits this load" and not re.search(r"DRV\d|SHP\d", json.dumps({k: v for k, v in cases[0].items() if k not in ("case_id", "recent")}))
    cid = cases[0]["case_id"]
    assert client.post(f"/demo/api/cases/{cid}/claim", json={"coordinator": "Asha Verma"}).status_code == 200
    clash = client.post(f"/demo/api/cases/{cid}/claim", json={"coordinator": "Ben Thomas"})
    assert clash.status_code == 409 and clash.json()["claimed_by"] == "Asha Verma"
    wrong = client.post(f"/demo/api/cases/{cid}/resolve", json={"coordinator": "Ben Thomas", "resolution_code": "ADVISED_DRIVER", "note": "x"})
    assert wrong.status_code == 409
    ok = client.post(f"/demo/api/cases/{cid}/resolve", json={"coordinator": "Asha Verma", "resolution_code": "ASSIGNED_SLOT", "note": "We booked dock D6 at 14:00."})
    assert ok.status_code == 200
    state = client.get(f"/demo/api/driver/{pradeep}/state").json()
    ops = [m for m in state["messages"] if m["who"] == "operations"]
    assert len(ops) == 2 and "We booked dock D6 at 14:00." in ops[-1]["text"]
    assert state["case"]["status"] == "RESOLVED"


# --- model switch, reset, validation ----------------------------------------------------------------------------
def test_without_a_configured_model_the_chat_is_off_and_there_is_no_stand_in(tmp_path: Path) -> None:
    # No model configured: the page says so, chat is refused, nothing is created, and the rest of the console still works.
    app, _ = build_demo_app(tmp_path / "nomodel")
    client = TestClient(app)
    cfg = client.get("/demo/api/config").json()
    assert cfg["chat_enabled"] is False and cfg["model"] == "" and "model_mode" not in cfg
    r = client.post("/demo/api/chat", json={"driver_id": driver_id(client, "Manoj Sharma"), "text": "my truck is 60 minutes late"})
    assert r.status_code == 409 and r.json()["reason_code"] == "NO_MODEL_CONFIGURED"
    assert client.get("/demo/api/cases").json()["cases"] == []  # no hand-off case was opened by the refusal
    assert client.get("/demo/api/board").status_code == 200
    assert client.post("/demo/api/reset").status_code == 200


def test_the_page_has_no_rule_based_assistant_switch() -> None:
    html = (Path(__file__).resolve().parents[1] / "src" / "setuhaul" / "demo" / "index.html").read_text(encoding="utf-8")
    assert "Rule-based" not in html and "modelStub" not in html and "/demo/api/model" not in html


def test_the_model_switch_endpoint_no_longer_exists(demo) -> None:  # type: ignore[no-untyped-def]
    client, _ = demo
    assert client.post("/demo/api/model", json={"mode": "stub"}).status_code in (404, 405)


def test_chat_uses_the_configured_gateway(tmp_path: Path) -> None:
    from setuhaul.model_gateway import ModelResponse, ScriptedGateway

    app, _deps = build_demo_app(tmp_path / "live", live_factory=lambda: ScriptedGateway([ModelResponse(text="Hello from the live model.")]))
    client = TestClient(app)
    assert client.get("/demo/api/config").json()["chat_enabled"] is True
    r = client.post("/demo/api/chat", json={"driver_id": driver_id(client, "Manoj Sharma"), "text": "hello there"}).json()
    assert r["reply"] == "Hello from the live model."


def test_reset_restores_the_schedule_the_clock_and_clears_conversations(demo) -> None:  # type: ignore[no-untyped-def]
    client, _ = demo
    manoj = driver_id(client, "Manoj Sharma")
    say(client, manoj, "my truck is 60 minutes late")
    say(client, manoj, "I will take the first one")
    say(client, driver_id(client, "Pradeep Jat"), "my truck is 70 minutes late")
    assert client.post("/demo/api/reset").status_code == 200
    assert client.get(f"/demo/api/driver/{manoj}/state").json()["messages"] == []
    assert client.get("/demo/api/cases").json()["cases"] == []
    assert not any(s["status"] == "HELD" for d in client.get("/demo/api/board").json()["docks"] for s in d["slots"])
    assert client.get("/demo/api/config").json()["clock"]["text"].endswith("09:35:00 IST")


def test_bad_input_is_rejected_cleanly(demo) -> None:  # type: ignore[no-untyped-def]
    client, _ = demo
    assert client.post("/demo/api/chat", json={"driver_id": "NOPE", "text": "hi"}).json()["reason_code"] == "UNKNOWN_DRIVER"
    assert client.post("/demo/api/chat", json={"driver_id": "x", "text": ""}).status_code == 422
    assert client.post("/demo/api/clock", json={"seconds": 0}).status_code == 422
    assert client.get("/demo/api/board", params={"facility_id": "NOPE"}).status_code == 404


# --- an empty dock board, and resets that keep working ---------------------------------------------------------
def statuses(client: TestClient) -> set[str]:
    return {s["status"] for d in client.get("/demo/api/board").json()["docks"] for s in d["slots"]}


def test_an_empty_board_has_every_slot_free_and_loads_without_bookings(tmp_path: Path) -> None:
    app, _ = build_demo_app(tmp_path / "fresh", fresh=True, live_factory=StubGateway)
    client = TestClient(app)
    assert client.get("/demo/api/config").json()["fresh"] is True
    assert "BOOKED" not in statuses(client) and "HELD" not in statuses(client)
    assert all(s["appointment"] is None for d in client.get("/demo/api/drivers").json()["drivers"] for s in d["shipments"])


def test_a_driver_can_book_from_an_empty_board(tmp_path: Path) -> None:
    # A load with no appointment at all still gets options, a hold and a confirmation on the empty board.
    app, _ = build_demo_app(tmp_path / "fresh2", fresh=True, live_factory=StubGateway)
    client = TestClient(app)
    manoj = driver_id(client, "Manoj Sharma")
    assert "These slots are available" in say(client, manoj, "my truck is 60 minutes late")["reply"]
    assert "holding" in say(client, manoj, "1")["reply"].lower()
    assert "confirmed" in say(client, manoj, "confirm")["reply"].lower()
    assert "BOOKED" in statuses(client)


def test_reset_can_switch_between_the_supplied_schedule_and_an_empty_board(demo) -> None:  # type: ignore[no-untyped-def]
    client, _ = demo
    assert "BOOKED" in statuses(client)  # the supplied schedule has bookings
    assert client.post("/demo/api/reset", json={"fresh": True}).json()["fresh"] is True
    assert "BOOKED" not in statuses(client)
    assert client.post("/demo/api/reset", json={}).json()["fresh"] is True  # no choice given: the mode is kept
    assert "BOOKED" not in statuses(client)
    assert client.post("/demo/api/reset", json={"fresh": False}).json()["fresh"] is False
    assert "BOOKED" in statuses(client)


def test_reset_keeps_working_while_the_page_keeps_reading(demo) -> None:  # type: ignore[no-untyped-def]
    # The page polls every few seconds; a reset used to fail (HTTP 500) when it clashed with a read on Windows.
    import threading

    client, _ = demo
    stop = threading.Event()

    def poll() -> None:
        while not stop.is_set():
            client.get("/demo/api/board")
            client.get("/demo/api/cases")

    t = threading.Thread(target=poll)
    t.start()
    try:
        codes = [client.post("/demo/api/reset", json={}).status_code for _ in range(6)]
    finally:
        stop.set()
        t.join()
    assert codes == [200] * 6
