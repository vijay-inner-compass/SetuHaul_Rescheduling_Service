"""One held slot at a time: held after a delay report, confirmed only by the driver, declined to get the next."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import re
from datetime import timedelta
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


def test_no_releases_and_holds_the_next(chat: Chat) -> None:
    original = chat.current()
    chat.say(LATE)
    first = chat.held()[0]["slot_id"]
    reply = chat.say("No")
    held = chat.held()
    assert len(held) == 1 and held[0]["slot_id"] != first and "I am holding" in reply
    assert chat.current() == original


def test_after_the_stored_options_a_new_search_skips_what_was_offered(chat: Chat) -> None:
    chat.say(LATE)
    seen = [chat.held()[0]["slot_id"]]
    for _ in range(4):
        chat.say("No")
        assert len(chat.held()) == 1
        seen.append(chat.held()[0]["slot_id"])
    assert len(set(seen)) == len(seen)


def test_nothing_left_hands_off_and_keeps_the_original_booking(chat: Chat) -> None:
    original = chat.current()
    chat.say(LATE)
    reply = ""
    for _ in range(60):
        reply = chat.say("No")
        if not chat.held():
            break
        chat.deps.redis.flushall()  # many messages in a row: not what the rate limit is for here
    assert chat.held() == [] and chat.current() == original
    assert chat.rows("select count(*) from escalation_cases where reason_code='NO_SLOT'")[0][0] == 1 and "I am holding" not in reply


def test_a_yes_containing_the_word_no_confirms(chat: Chat) -> None:
    chat.say(LATE)
    slot = chat.held()[0]["slot_id"]
    chat.say("no problem, yes confirm")
    assert chat.current() == slot


def test_yes_after_expiry_does_not_confirm(chat: Chat) -> None:
    original = chat.current()
    chat.say(LATE)
    chat.deps.clock.advance(timedelta(seconds=181))
    chat.say("Yes")
    # The lapsed row waits for the sweep; what matters is that nothing was booked from it.
    assert chat.current() == original
    assert chat.rows("select count(*) from appointments where appointment_id like 'APT-H-%' and appointment_status='CONFIRMED'")[0][0] == 0


@pytest.mark.parametrize("text", ["yes, no problem", "confirm, there is no issue", "haan, koi problem nahi"])
def test_an_agreeing_message_can_never_decline(text: str) -> None:
    from setuhaul.tools import sounds_like_agreement

    assert sounds_like_agreement(text)
    assert not sounds_like_agreement("No") and not sounds_like_agreement("no, next one please")
