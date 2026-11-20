"""Edge cases and failure scenarios added with the documentation pass (see docs/scenario-catalogue.md).

Each test names one situation a reader might ask "what if...?" about and pins the answer.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from setuhaul.clock import FakeClock
from tests.conv_helpers import Chat, facts_from, say_text, tc, tc_dyn, tool_json
from tests.helpers import seed_everything_known
from tests.test_api import env, msg, post  # noqa: F401  (the API fixture and helpers)

LATE_60 = {"kind": "late_by", "minutes": 60}


@pytest.fixture()
def chat(db: sqlite3.Connection, clock: FakeClock, tmp_path: Path) -> Chat:
    # A conversation for driver DRV006 on the standard evidence.
    seed_everything_known(db)
    return Chat(db, clock, tmp_path)


# --- the numbered menu ----------------------------------------------------------------------------------
def test_when_nothing_fits_there_is_a_hand_off_and_no_menu(db: sqlite3.Connection, clock: FakeClock, tmp_path: Path) -> None:
    # SHP1014 is a 21,000 kg load with a 75-minute unload that no single slot can take.
    seed_everything_known(db)
    c = Chat(db, clock, tmp_path, driver="DRV014", thread="THR004")
    r = c.say("SHP1014 is 70 minutes late", [
        tc("search_slot_options", shipment_id="SHP1014", report={"kind": "late_by", "minutes": 70}), say_text("here are some options")])
    # The system hands the case over and shows no list, whatever the model wrote.
    assert r.status == "HANDOFF" and "These slots are available" not in r.text and "1. Dock" not in r.text
    # No menu was even stored, because a search with no feasible slot returns before anything is recorded.
    assert c.rows("select count(*) from option_sets") == [(0,)]


def test_two_searches_in_one_message_show_only_the_latest_menu(chat: Chat) -> None:
    # The model searches twice in one turn; the driver must see (and be able to pick from) the latest menu only.
    r = chat.say("SHP1006 is running 60 minutes late", [
        tc("search_slot_options", shipment_id="SHP1006", report=LATE_60, call_id="a"),
        tc("search_slot_options", shipment_id="SHP1006", report=LATE_60, call_id="b"),
        say_text("whatever")])
    latest = chat.rows("select option_set_id from option_sets order by rowid desc limit 1")[0][0]
    items = chat.rows("select count(*) from option_set_items where option_set_id=?", latest)[0][0]
    assert len([ln for ln in r.text.split("\n") if ln[:2] in ("1.", "2.", "3.")]) == items


def test_a_rank_outside_the_menu_is_rejected_and_holds_nothing(chat: Chat) -> None:
    # Option 5 does not exist (the schema allows 1 to 3): the call is refused before any service runs.
    chat.say("SHP1006 is running 60 minutes late", [tc("search_slot_options", shipment_id="SHP1006", report=LATE_60), say_text("x")])
    chat.say("option 5 please", [
        tc_dyn("request_hold", lambda m: {"option_set_id": facts_from(m)["latest_option_set_id"], "rank": 5, "driver_quote": "option 5"}),
        say_text("sorry")])
    assert tool_json(chat.last_messages(), "request_hold")["error"] == "INVALID_ARGUMENTS"
    assert chat.held() == 0


def test_a_new_search_replaces_the_menu_a_number_refers_to(chat: Chat) -> None:
    # After a second search the old menu id can no longer be held, so "option 1" cannot silently mean the old line.
    chat.say("SHP1006 is running 60 minutes late", [tc("search_slot_options", shipment_id="SHP1006", report=LATE_60), say_text("x")])
    first = chat.rows("select option_set_id from option_sets order by rowid desc limit 1")[0][0]
    chat.say("actually 90 minutes late", [
        tc("search_slot_options", shipment_id="SHP1006", report={"kind": "late_by", "minutes": 90}), say_text("x")])
    chat.say("take option 1", [
        tc_dyn("request_hold", lambda m: {"option_set_id": first, "rank": 1, "driver_quote": "option 1"}), say_text("ok")])
    assert tool_json(chat.last_messages(), "request_hold")["status"] == "NOT_LATEST_OPTION_SET"
    assert chat.held() == 0


# --- the HTTP door ------------------------------------------------------------------------------------------
def test_a_message_of_exactly_2000_characters_is_accepted_and_2001_is_refused(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    _, client, calls, *_ = env
    assert post(client, msg("a" * 2001, mid="LONG-1")).status_code == 422  # one over the limit
    assert post(client, msg("a" * 2000, mid="LONG-2")).status_code == 200  # exactly at the limit
    assert calls.n == 1  # only the accepted message reached the model


def test_an_empty_message_is_refused_before_the_model(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    _, client, calls, *_ = env
    assert post(client, msg("", mid="EMPTY-1")).status_code == 422
    assert calls.n == 0
