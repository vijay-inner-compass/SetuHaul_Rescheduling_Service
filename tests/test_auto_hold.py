"""One held slot at a time: held after a delay report, confirmed only by the driver, declined to get the next."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from setuhaul.db import open_connection
from setuhaul.demo_ui import build_demo_app
from setuhaul.language import say
from setuhaul.stub_model import StubGateway
from tests.test_demo_driver import _driver

LATE = "I will be about two hours late"


class Chat:
    """DRV006 talking to the real pipeline through the demo driver account."""

    def __init__(self, tmp_path: Path) -> None:
        self.app, self.deps = build_demo_app(tmp_path, live_factory=StubGateway)
        self.client = _driver(self.app)

    def say(self, text: str) -> str:
        r = self.client.post("/demo/driver/api/message", json={"text": text})
        assert r.status_code == 200, r.text
        return str(r.json()["reply"])

    def rows(self, sql: str) -> list[Any]:
        conn = open_connection(path=self.deps.db_path, readonly=True)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def held(self) -> list[Any]:
        return self.rows("select a.appointment_id, a.slot_id, a.shipment_id from appointments a join appointment_hold_meta m using(appointment_id) "
                         "where a.appointment_status='HELD' and m.driver_id='DRV006'")

    def current(self) -> Any:
        return self.rows("select slot_id from appointments where shipment_id='SHP1006' and is_current=1")[0]["slot_id"]


@pytest.fixture()
def chat(tmp_path: Path) -> Chat:
    return Chat(tmp_path)


def test_delay_report_holds_the_first_option(chat: Chat) -> None:
    original = chat.current()
    reply = chat.say(LATE)
    assert len(chat.held()) == 1
    assert "Jaipur" in reply and "dock D" in reply and "Yes" in reply
    # One slot, no numbered menu, and nothing booked yet.
    assert re.search(r"^\s*[123]\.", reply, re.MULTILINE) is None
    assert chat.current() == original


def test_hold_message_in_hindi_and_hinglish_names_the_facility() -> None:
    for lang in ("en", "hi", "hinglish"):
        text = say("hold_placed", lang, facility="FACILITY-X", dock="D1", date="04 Aug 2026", start="13:00", end="14:00", until="09:38")
        assert "FACILITY-X" in text and "D1" in text and "09:38" in text


def test_yes_in_the_next_message_confirms(chat: Chat) -> None:
    chat.say(LATE)
    slot = chat.held()[0]["slot_id"]
    chat.say("Yes, confirm")
    assert chat.held() == [] and chat.current() == slot


def test_no_hold_possible_moves_to_the_next_option(chat: Chat, monkeypatch: pytest.MonkeyPatch) -> None:
    from setuhaul import tools

    real, tried = tools.acquire_hold, []

    def taken_first(conn, driver_id, thread_id, shipment_id, option_set_id, rank, clock, **kw):  # type: ignore[no-untyped-def]
        # Someone else books the first line between the search and the hold.
        tried.append(rank)
        if rank == 1:
            from setuhaul.booking import HoldResult, OutcomeClass

            return HoldResult(status="SLOT_CONFLICT", outcome=OutcomeClass.BUSINESS_CONFLICT)
        return real(conn, driver_id, thread_id, shipment_id, option_set_id, rank, clock, **kw)

    monkeypatch.setattr(tools, "acquire_hold", taken_first)
    chat.say(LATE)
    assert tried == [1, 2] and len(chat.held()) == 1
