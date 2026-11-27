"""Edge cases and failure scenarios added with the documentation pass (see docs/scenario-catalogue.md).

Each test names one situation a reader might ask "what if...?" about and pins the answer.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from setuhaul.clock import FakeClock
from setuhaul.stub_model import StubGateway  # a scripted stand-in model, for tests only
from tests.auth_helpers import demo_client
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


def test_two_searches_in_one_message_leave_one_hold_from_the_latest_search(chat: Chat) -> None:
    # The model searches twice in one turn; the driver ends with exactly one hold, taken from the latest search.
    r = chat.say("SHP1006 is running 60 minutes late", [
        tc("search_slot_options", shipment_id="SHP1006", report=LATE_60, call_id="a"),
        tc("search_slot_options", shipment_id="SHP1006", report=LATE_60, call_id="b"),
        say_text("whatever")])
    latest = chat.rows("select option_set_id from option_sets order by rowid desc limit 1")[0][0]
    offered = [row[0] for row in chat.rows("select slot_id from option_set_items where option_set_id=?", latest)]
    held = chat.rows("select slot_id from appointments where appointment_status='HELD'")
    assert len(held) == 1 and held[0][0] in offered and r.text.count("I am holding") == 1


def test_a_rank_outside_the_offer_is_rejected_and_changes_no_hold(chat: Chat) -> None:
    # Option 5 does not exist (the schema allows 1 to 3): the call is refused before any service runs.
    chat.say("SHP1006 is running 60 minutes late", [tc("search_slot_options", shipment_id="SHP1006", report=LATE_60), say_text("x")])
    chat.say("option 5 please", [
        tc_dyn("request_hold", lambda m: {"option_set_id": facts_from(m)["latest_option_set_id"], "rank": 5, "driver_quote": "option 5"}),
        say_text("sorry")])
    assert tool_json(chat.last_messages(), "request_hold")["error"] == "INVALID_ARGUMENTS"
    # Only the system's own hold from the search: the refused call changed nothing.
    assert chat.held() == 1


def test_a_new_search_replaces_the_menu_a_number_refers_to(chat: Chat) -> None:
    # After a second search the old menu id can no longer be held, so "option 1" cannot silently mean the old line.
    chat.say("SHP1006 is running 60 minutes late", [tc("search_slot_options", shipment_id="SHP1006", report=LATE_60), say_text("x")])
    first = chat.rows("select option_set_id from option_sets order by rowid desc limit 1")[0][0]
    chat.say("actually 90 minutes late", [
        tc("search_slot_options", shipment_id="SHP1006", report={"kind": "late_by", "minutes": 90}), say_text("x")])
    chat.say("take option 1", [
        tc_dyn("request_hold", lambda m: {"option_set_id": first, "rank": 1, "driver_quote": "option 1"}), say_text("ok")])
    assert tool_json(chat.last_messages(), "request_hold")["status"] == "NOT_LATEST_OPTION_SET"
    assert chat.held() == 1  # the newer search's own hold, not the stale line


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
    # Only the system's own hold from the search: the refused call changed nothing.
    assert chat.held() == 1


def test_the_rule_based_assistant_books_on_a_plain_yes(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    # The demo's stand-in assistant must follow the hold message: "Yes" books the held slot.
    _, client, *_ = env
    assert "I am holding" in post(client, msg("my truck is 60 minutes late", mid="N-1")).json()["reply"]
    r = post(client, msg("Yes", mid="N-2")).json()
    assert r["reply"].startswith("Your appointment is confirmed") and r["status"] == "REPLIED"


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
    assert "I am holding" in body["reply"] or body["status"] == "HANDOFF"


# --- cancelling and releasing with the rule-based assistant; the dock board must follow ------------------------
def _board_statuses(client) -> set[str]:  # type: ignore[no-untyped-def]
    return {s["status"] for d in client.get("/demo/api/board").json()["docks"] for s in d["slots"]}


def test_cancelling_a_confirmed_appointment_frees_its_slot_on_the_board(tmp_path: Path) -> None:

    from setuhaul.demo_ui import build_demo_app

    client = demo_client(build_demo_app(tmp_path / "cx", fresh=True, live_factory=StubGateway, driver_simulation=True)[0])
    did = next(d["id"] for d in client.get("/demo/api/drivers").json()["drivers"] if d["name"] == "Manoj Sharma")

    def say(t: str) -> str:
        return client.post("/demo/api/chat", json={"driver_id": did, "text": t}).json()["reply"]

    say("my truck is 60 minutes late")
    say("1")
    assert "confirmed" in say("confirm").lower() and "BOOKED" in _board_statuses(client)
    assert "reply yes to cancel" in say("cancel my appointment").lower() and "BOOKED" in _board_statuses(client)
    assert "has been cancelled" in say("yes").lower()
    assert "BOOKED" not in _board_statuses(client) and "HELD" not in _board_statuses(client)


def test_keeping_the_original_slot_releases_the_hold_on_the_board(tmp_path: Path) -> None:

    from setuhaul.demo_ui import build_demo_app

    client = demo_client(build_demo_app(tmp_path / "rel", fresh=True, live_factory=StubGateway, driver_simulation=True)[0])
    did = next(d["id"] for d in client.get("/demo/api/drivers").json()["drivers"] if d["name"] == "Manoj Sharma")

    def say(t: str) -> str:
        return client.post("/demo/api/chat", json={"driver_id": did, "text": t}).json()["reply"]

    say("my truck is 60 minutes late")
    say("1")
    assert "HELD" in _board_statuses(client)
    assert "released" in say("keep my original slot").lower()
    assert "HELD" not in _board_statuses(client)


def test_cancelling_with_nothing_booked_says_so(tmp_path: Path) -> None:

    from setuhaul.demo_ui import build_demo_app

    client = demo_client(build_demo_app(tmp_path / "none", fresh=True, live_factory=StubGateway, driver_simulation=True)[0])
    did = next(d["id"] for d in client.get("/demo/api/drivers").json()["drivers"] if d["name"] == "Manoj Sharma")
    reply = client.post("/demo/api/chat", json={"driver_id": did, "text": "cancel my appointment"}).json()["reply"]
    assert "could not find an appointment" in reply.lower()


# --- the truck's position changes -------------------------------------------------------------------------------
def _gps_demo(tmp_path: Path):  # type: ignore[no-untyped-def]

    from setuhaul.demo_ui import build_demo_app

    client = demo_client(build_demo_app(tmp_path / "gps", fresh=True, live_factory=StubGateway, driver_simulation=True)[0])
    did = next(d["id"] for d in client.get("/demo/api/drivers").json()["drivers"] if d["name"] == "Manoj Sharma")
    fac = client.get(f"/demo/api/driver/{did}/gps").json()["facility"]

    def ping(km: float) -> None:
        r = client.post("/demo/api/gps", json={"driver_id": did, "lat": fac["lat"] + km / 111.2, "lon": fac["lon"], "age_minutes": 0})
        assert r.status_code == 200, r.text

    def say(t: str) -> str:
        return client.post("/demo/api/chat", json={"driver_id": did, "text": t}).json()["reply"]

    def held() -> bool:
        return any(s["status"] == "HELD" for d in client.get("/demo/api/board").json()["docks"] for s in d["slots"])

    def chat() -> list[str]:
        return [m["text"] for m in client.get(f"/demo/api/driver/{did}/state").json()["messages"]]

    return client, ping, say, held, chat


def test_a_new_ping_far_away_releases_the_hold_at_once_and_tells_the_driver(tmp_path: Path) -> None:
    # The truck is 20 km away, gets a 12:00 hold, then a new ping puts it 300 km away: it cannot reach 12:00, so the dock is freed.
    _, ping, say, held, chat = _gps_demo(tmp_path)
    ping(20)
    say("my truck is 60 minutes late")
    assert "holding" in say("1").lower() and held()
    ping(300)
    assert not held()  # the slot is free for others straight away
    assert any("position has changed" in t and "released the hold" in t for t in chat())  # and the driver was told


def test_a_new_ping_that_is_still_close_keeps_the_hold(tmp_path: Path) -> None:
    _, ping, say, held, chat = _gps_demo(tmp_path)
    ping(20)
    say("my truck is 60 minutes late")
    say("1")
    ping(40)  # a different location, still comfortably in time
    assert held() and not any("position has changed" in t for t in chat())


def test_a_ping_that_moves_the_truck_away_before_the_driver_picks_blocks_the_hold(tmp_path: Path) -> None:
    # The menu was built while the truck was close; the driver replies "1" after it moved far away: no hold, system says why.
    _, ping, say, held, _ = _gps_demo(tmp_path)
    ping(20)
    say("my truck is 60 minutes late")
    ping(300)
    reply = say("1")
    assert "position has changed" in reply and "Nothing is held" in reply and not held()


def test_a_ping_that_moves_the_truck_closer_changes_nothing(tmp_path: Path) -> None:
    # Closer is never a reason to refuse: the floor only ever makes the arrival later, never earlier.
    _, ping, say, held, _ = _gps_demo(tmp_path)
    ping(150)
    say("my truck is 60 minutes late")
    ping(5)
    assert "holding" in say("1").lower() and held()


def test_confirming_after_a_far_ping_that_was_not_reassessed_still_refuses(chat: Chat) -> None:
    # Belt and braces: even if a ping is stored without the immediate re-check, confirming judges the slot from the truck's position.
    from datetime import timedelta

    from setuhaul.gps import store_ping
    from setuhaul.schemas import GpsPing

    chat.say("SHP1006 is running 60 minutes late", [tc("search_slot_options", shipment_id="SHP1006", report=LATE_60), say_text("x")])
    chat.say("take option 1", [
        tc_dyn("request_hold", lambda m: {"option_set_id": facts_from(m)["latest_option_set_id"], "rank": 1, "driver_quote": "option 1"}),
        say_text("ok")])
    assert chat.held() == 1
    fac = chat.rows("select f.lat, f.lon from shipments s join facilities f on f.facility_id = s.destination_facility_id "
                    "where s.shipment_id='SHP1006'")[0]
    chat.clock.advance(timedelta(seconds=20))
    store_ping(chat.db, "SHP1006", GpsPing(lat=fac[0] + 300 / 111.2, lon=fac[1], recorded_at=chat.clock.now()), chat.clock, "TEST")
    chat.db.commit()
    r = chat.say("yes, confirm it", [
        tc_dyn("confirm_reschedule", lambda m: {"hold_appointment_id": facts_from(m)["live_hold"]["appointment_id"], "driver_quote": "confirm it"}),
        say_text("done")])
    assert "position has changed" in r.text and not r.verified_confirmed
    assert chat.held() == 0  # the hold was released
    assert chat.rows("select appointment_status from appointments where appointment_id='APT1006'") == [("CONFIRMED",)]  # original stands


# --- a position update is confirmed, and a closer truck is offered earlier slots ----------------------------------------
def _booked_or_held_starts(client) -> list[str]:  # type: ignore[no-untyped-def]
    return sorted(s["start"] for d in client.get("/demo/api/board").json()["docks"] for s in d["slots"] if s["status"] in ("BOOKED", "HELD"))


def test_every_ping_is_confirmed_to_the_driver_with_distance_and_drive_time(tmp_path: Path) -> None:
    _, ping, _, _, chat = _gps_demo(tmp_path)
    ping(24)
    msgs = [t for t in chat() if "your new position" in t]
    assert len(msgs) == 1 and "about 24 km" in msgs[0] and "minutes away" in msgs[0] and "arrive around" in msgs[0]


def test_a_closer_truck_is_offered_one_held_earlier_slot_and_can_switch_its_booking(tmp_path: Path) -> None:
    # Far away (165 km) the earliest reachable slot is mid-afternoon; at 24 km much earlier slots open up.
    client, ping, say, held, chat = _gps_demo(tmp_path)
    ping(165)
    say("my truck is 60 minutes late")
    assert "confirmed" in say("yes confirm").lower()
    late = _booked_or_held_starts(client)[0]
    ping(24)  # the truck is now much closer
    offer = [t for t in chat() if "earlier slot" in t]
    # One slot, already held, answered with Yes or No: never a numbered list.
    assert len(offer) == 1 and held()
    assert "Reply Yes" in offer[0] and "No for the next" in offer[0] and "1. Dock" not in offer[0]
    assert "SetuHaul Jaipur Distribution Centre" in offer[0]
    first_offered = re.search(r"(\d\d:\d\d)-\d\d:\d\d", offer[0]).group(1)  # type: ignore[union-attr]
    assert first_offered < late  # genuinely earlier than the slot it had
    # The confirmed booking stays until the driver agrees; agreeing swaps them.
    assert late in _booked_or_held_starts(client)
    assert "confirmed" in say("yes confirm").lower()
    assert _booked_or_held_starts(client) == [first_offered]  # the old late slot is free again, only the new one is booked


def test_a_declined_earlier_offer_keeps_the_booking_and_is_not_repeated(tmp_path: Path) -> None:
    client, ping, say, _held, chat = _gps_demo(tmp_path)
    ping(165)
    say("my truck is 60 minutes late")
    say("yes confirm")
    late = _booked_or_held_starts(client)[0]
    ping(24)
    say("No")
    assert late in _booked_or_held_starts(client)
    ping(23)
    assert len([t for t in chat() if "earlier slot" in t]) == 1


def test_the_same_offer_is_not_repeated_for_every_ping(tmp_path: Path) -> None:
    _client, ping, say, _, chat = _gps_demo(tmp_path)
    ping(165)
    say("my truck is 60 minutes late")
    say("yes confirm")
    ping(24)
    ping(23)
    ping(22)
    assert len([t for t in chat() if "earlier slot" in t]) == 1  # one offer, three confirmations
    assert len([t for t in chat() if "your new position" in t]) == 4  # 165, 24, 23, 22


def test_no_offer_when_the_current_slot_is_already_as_early_as_it_gets(tmp_path: Path) -> None:
    _, ping, say, _, chat = _gps_demo(tmp_path)
    ping(20)
    say("my truck is 60 minutes late")
    say("yes confirm")
    ping(5)  # closer, but there is nothing at least 30 minutes earlier than the slot it already has
    assert not any("earlier slot" in t for t in chat())


def test_a_search_from_far_away_does_not_offer_slots_the_truck_cannot_reach(tmp_path: Path) -> None:
    # At 300 km (about 10 hours) nothing before about 19:30 can be reached, so the first menu must not show 12:00 or 13:00.
    _, ping, say, _, _ = _gps_demo(tmp_path)
    ping(300)
    reply = say("my truck is 60 minutes late")
    starts = re.findall(r"(\d\d:\d\d)-\d\d:\d\d", reply)
    assert (starts and all(s >= "19:00" for s in starts)) or "operations" in reply.lower()


def test_a_position_update_reaches_the_load_with_the_late_booking_even_when_it_is_not_the_first_load(tmp_path: Path) -> None:
    # Sandeep Meena has two open loads; the ping attaches to the first (09:00), his 18:00 booking is on the second. The truck has one
    # position, so moving it close must still offer earlier slots for the 18:00 load, naming its order.

    from setuhaul.demo_ui import build_demo_app

    client = demo_client(build_demo_app(tmp_path / "two", live_factory=StubGateway, driver_simulation=True)[0])  # the supplied schedule, with its bookings
    sandeep = next(d for d in client.get("/demo/api/drivers").json()["drivers"] if d["name"] == "Sandeep Meena")
    late = next(s for s in sandeep["shipments"] if s["appointment"] and s["appointment"].startswith("18:00"))
    fac = client.get(f"/demo/api/driver/{sandeep['id']}/gps").json()["facility"]
    for km in (165, 24):
        r = client.post("/demo/api/gps", json={"driver_id": sandeep["id"], "lat": fac["lat"] + km / 111.2, "lon": fac["lon"], "age_minutes": 0})
        assert r.status_code == 200
    texts = [m["text"] for m in client.get(f"/demo/api/driver/{sandeep['id']}/state").json()["messages"]]
    offers = [t for t in texts if "earlier slot" in t]
    # One question at a time: while the first earlier slot is held, a closer ping does not swap it under the driver.
    assert len(offers) == 1 and offers[0].startswith(f"For order {late['order']}:") and "Reply Yes" in offers[0]
    assert re.search(r"(\d\d:\d\d)-\d\d:\d\d", offers[0]).group(1) < "18:00"  # type: ignore[union-attr]
    assert len([t for t in texts if "your new position" in t]) == 2  # each ping confirmed once, not once per load
