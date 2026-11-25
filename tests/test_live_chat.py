"""Chat order, live refresh of the warehouse screen, and the driver's booking card."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from setuhaul.auth.pages import _chat_browser
from setuhaul.db import open_connection
from setuhaul.demo_ui import build_demo_app
from setuhaul.stub_model import StubGateway
from tests.test_demo_driver import _driver
from tests.test_driver_chat_window import THREAD, _allocate, _project_of

pytest_plugins = ["tests.test_driver_chat_window"]


def _pair(world) -> None:  # type: ignore[no-untyped-def]
    # A question and its answer with the SAME timestamp, whose random ids sort the wrong way round.
    conn = open_connection(path=world.path)
    for mid, who, text in (("MSG-ZZZ", "DRIVER", "first: I am late"), ("MSG-AAA", "AGENT", "second: these slots")):
        conn.execute("insert into chat_messages(chat_message_id, thread_id, sender_type, message_text, message_ts) values (?,?,?,?,?)",
                     (mid, THREAD, who, text, world.clock.now().isoformat()))
    conn.commit()
    conn.close()


def test_a_reply_never_sorts_before_its_question(world) -> None:  # type: ignore[no-untyped-def]
    world.clock.advance(timedelta(hours=1))
    _allocate(world, "wh_a", _project_of(world, "wh_a"))
    world.clock.advance(timedelta(minutes=1))
    _pair(world)
    for who in ("wh_a", "admin"):
        drivers = world.client(who).get("/v1/warehouse/driver-chats").json()["drivers"]
        texts = [m["text"] for d in drivers if d["driver_id"] == "DRV006" for m in d["messages"]]
        assert texts[-2:] == ["first: I am late", "second: these slots"], who


def test_driver_state_says_which_facility_dock_and_status(tmp_path: Path) -> None:
    app, _ = build_demo_app(tmp_path, live_factory=StubGateway)
    booking = _driver(app).get("/demo/driver/api/state").json()["booking"]
    assert "Jaipur" in booking["facility"] and booking["order"].startswith("ORD-")
    assert "Dock D4" in booking["booked"] and "10:00" in booking["booked"]
    page = _driver(app).get("/demo/driver").text
    assert 'id="booking"' in page and "/demo/api/board" not in page


def test_warehouse_chats_can_be_refreshed_in_place() -> None:
    page = _chat_browser([{"driver_id": "DRV006", "driver_name": "Manoj", "messages": []}])
    assert 'data-driver="DRV006"' in page
    script = (Path(__file__).resolve().parents[1] / "src/setuhaul/auth/assets/warehouse.js").read_text(encoding="utf-8")
    assert "DOMParser" in script and "setInterval" in script and "document.hidden" in script


def test_action_buttons_still_work_after_the_live_refresh_replaces_them() -> None:
    # The refresh swaps whole sections in; a handler bound to each button at load would be lost with the old node.
    script = (Path(__file__).resolve().parents[1] / "src/setuhaul/auth/assets/common.js").read_text(encoding="utf-8")
    assert 'document.addEventListener("click"' in script and 'closest("button[data-api]")' in script
    assert 'querySelectorAll("button[data-api]")' not in script
