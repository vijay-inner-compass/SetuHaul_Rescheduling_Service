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


# --- replying with just a number (the menu says "Reply with the number") --------------------------------------
def test_a_reply_of_just_a_number_holds_that_menu_line(chat: Chat) -> None:
    # The driver sends "3" and nothing else; the model quotes "3". One character is enough when it IS the whole message.
    chat.say("SHP1006 is running 60 minutes late", [tc("search_slot_options", shipment_id="SHP1006", report=LATE_60), say_text("x")])
    third = chat.rows("select slot_id from option_set_items where displayed_rank=3 order by rowid desc limit 1")[0][0]
    r = chat.say("3", [
        tc_dyn("request_hold", lambda m: {"option_set_id": facts_from(m)["latest_option_set_id"], "rank": 3, "driver_quote": "3"}),
        say_text("ok")])
    assert r.verified_hold and chat.rows("select slot_id from appointments where appointment_status='HELD'") == [(third,)]


def test_a_one_character_quote_inside_a_longer_message_is_not_approval(chat: Chat) -> None:
    # "3" appears in the message, but the message is about something else, so it cannot authorise a hold.
    chat.say("SHP1006 is running 60 minutes late", [tc("search_slot_options", shipment_id="SHP1006", report=LATE_60), say_text("x")])
    chat.say("I have 3 trucks on this road", [
        tc_dyn("request_hold", lambda m: {"option_set_id": facts_from(m)["latest_option_set_id"], "rank": 3, "driver_quote": "3"}),
        say_text("ok")])
    assert tool_json(chat.last_messages(), "request_hold")["error"] == "APPROVAL_NOT_PRESENT"
    assert chat.held() == 0


def test_the_rule_based_assistant_understands_a_menu_number(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    # The demo's stand-in assistant must follow its own menu instruction: "3" after the menu places a hold.
    _, client, *_ = env
    assert "These slots are available" in post(client, msg("my truck is 60 minutes late", mid="N-1")).json()["reply"]
    r = post(client, msg("3", mid="N-2")).json()
    assert "holding" in r["reply"].lower() and r["status"] == "REPLIED"


# --- delays written as hours --------------------------------------------------------------------------------
@pytest.mark.parametrize(("text", "minutes"), [
    ("book my slot after 2 hours as i am running late", 120), ("an hour late", 60), ("half an hour late", 30),
    ("1 hour 30 minutes late", 90), ("1.5 hrs late", 90), ("2 ghante late", 120), ("60 minutes late", 60), ("hello", 0),
])
def test_the_rule_based_assistant_reads_delays_in_hours_and_minutes(text: str, minutes: int) -> None:
    from setuhaul.stub_model import _delay_minutes

    assert _delay_minutes(text.lower()) == minutes


def test_a_delay_given_in_hours_gets_an_answer_not_the_generic_question(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    # "book my slot after 2 hours as i am running late" used to get "How late are you...?" back.
    _, client, *_ = env
    body = post(client, msg("book my slot after 2 hours as i am running late", mid="H-1")).json()
    assert body["reply"] != "How late are you, and which shipment is it?" and body["status"] in ("REPLIED", "HANDOFF")
    assert "These slots are available" in body["reply"] or body["status"] == "HANDOFF"
