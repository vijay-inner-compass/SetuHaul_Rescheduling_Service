"""Code, not the model, decides that the driver agreed before a booking is confirmed or cancelled.

A model can call the right tool on the wrong message. Confirming needs a clear yes in the driver's own message;
cancelling takes two messages, the system asking in between.
"""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from setuhaul.clock import FakeClock
from setuhaul.tools import agrees_to_cancel, agrees_to_confirm
from tests.conv_helpers import Chat, facts_from, say_text, tc, tc_dyn
from tests.helpers import at, seed_everything_known

SEARCH = [tc("search_slot_options", shipment_id="SHP1006", report={"kind": "late_by", "minutes": 60}), say_text("x")]


@pytest.mark.parametrize("text", [
    "yes", "Yes please", "confirm it", "ok", "haan", "haan confirm kar do", "theek hai", "chalega", "go ahead", "book it",
    "हाँ", "कन्फर्म करें", "ठीक है", "no problem, yes",
])
def test_a_clear_yes_confirms(text: str) -> None:
    assert agrees_to_confirm(text)


@pytest.mark.parametrize("text", [
    "no", "nahi chalega, mat confirm karna", "don't confirm", "dont confirm it", "what time is it now?", "is that ok?",
    "ok but not that one", "yes, but can you make it later?", "cancel it", "ok cancel", "नहीं", "कन्फर्म मत कीजिए",
    "wait, ok one minute", "my truck is stuck", "kya yeh confirm hai",
])
def test_anything_else_does_not_confirm(text: str) -> None:
    assert not agrees_to_confirm(text)


@pytest.mark.parametrize("text", ["yes", "yes cancel it", "haan cancel kar do", "cancel", "ok", "हाँ", "रद्द कर दीजिए"])
def test_a_clear_yes_cancels(text: str) -> None:
    assert agrees_to_cancel(text)


@pytest.mark.parametrize("text", [
    "no", "no, keep it", "nahi, cancel mat karo", "don't cancel", "what is my slot?", "ok keep my appointment", "नहीं",
])
def test_anything_else_does_not_cancel(text: str) -> None:
    assert not agrees_to_cancel(text)


# --- confirming -----------------------------------------------------------------------------------------------
@pytest.fixture()
def chat(db: sqlite3.Connection, clock: FakeClock, tmp_path: Path) -> Chat:
    seed_everything_known(db)
    return Chat(db, clock, tmp_path)


def _confirm(quote: str):  # type: ignore[no-untyped-def]
    return [tc_dyn("confirm_reschedule", lambda m: {"hold_appointment_id": facts_from(m)["live_hold"]["appointment_id"],
                                                    "driver_quote": quote}), say_text("Your appointment is confirmed.")]


@pytest.mark.parametrize("text,quote", [("nahi chalega, mat confirm karna", "confirm"), ("what time is it now?", "time")])
def test_a_confirm_call_on_a_message_that_does_not_agree_books_nothing(chat: Chat, text: str, quote: str) -> None:
    chat.say("SHP1006 is running 60 minutes late", SEARCH)
    r = chat.say(text, _confirm(quote))
    assert not r.verified_confirmed and chat.held() == 1
    assert chat.rows("select appointment_status, is_current from appointments where appointment_id='APT1006'") == [("CONFIRMED", 1)]
    assert "Yes" in r.text and "is confirmed" not in r.text


# --- cancelling -----------------------------------------------------------------------------------------------
def _cancel(quote: str):  # type: ignore[no-untyped-def]
    return [tc("cancel_appointment", shipment_id="SHP1017", driver_quote=quote), say_text("Done.")]


def _status(chat: Chat) -> str:
    return str(chat.rows("select appointment_status from appointments where appointment_id='APT1017'")[0][0])


@pytest.fixture()
def booked(db: sqlite3.Connection, clock: FakeClock, tmp_path: Path) -> Chat:
    seed_everything_known(db)
    return Chat(db, clock, tmp_path, driver="DRV001", thread="THR011")


def test_the_first_cancel_message_only_asks(booked: Chat) -> None:
    r = booked.say("what is my slot?", _cancel("slot"))
    assert _status(booked) == "CONFIRMED"
    assert "Reply Yes to cancel" in r.text and "Nothing has been cancelled" in r.text and "Done." not in r.text


def test_a_yes_in_the_next_message_cancels(booked: Chat) -> None:
    booked.say("please cancel my appointment", _cancel("cancel"))
    r = booked.say("yes", _cancel("yes"))
    assert _status(booked) == "CANCELLED" and r.text == "Your appointment has been cancelled. Reference APT1017."


def test_a_no_in_the_next_message_cancels_nothing(booked: Chat) -> None:
    booked.say("please cancel my appointment", _cancel("cancel"))
    r = booked.say("no, keep it", _cancel("keep it"))
    assert _status(booked) == "CONFIRMED" and "not cancelled" in r.text


def test_two_calls_in_one_message_do_not_cancel(booked: Chat) -> None:
    booked.say("cancel my appointment", [tc("cancel_appointment", shipment_id="SHP1017", driver_quote="cancel"),
                                         tc("cancel_appointment", call_id="t2", shipment_id="SHP1017", driver_quote="cancel"),
                                         say_text("Done.")])
    assert _status(booked) == "CONFIRMED"


def test_the_question_lapses_after_another_message(booked: Chat) -> None:
    booked.say("please cancel my appointment", _cancel("cancel"))
    booked.say("what is the weather", [say_text("I can only help with your delivery.")])
    booked.say("yes", _cancel("yes"))
    assert _status(booked) == "CONFIRMED"


def test_the_question_lapses_with_time(booked: Chat) -> None:
    booked.say("please cancel my appointment", _cancel("cancel"))
    booked.clock.advance(timedelta(minutes=11))
    booked.say("yes", _cancel("yes"))
    assert _status(booked) == "CONFIRMED"


def test_a_yes_to_the_cancel_question_cannot_confirm_a_hold(chat: Chat) -> None:
    chat.say("SHP1006 is running 60 minutes late", SEARCH)
    chat.db.execute("update consignee_commitments set penalty_applies=0, exceptional=0")
    chat.db.commit()
    chat.clock.set(at("06:00"))
    chat.say("SHP1006 is running 60 minutes late", SEARCH)
    chat.say("cancel my appointment", [tc("cancel_appointment", shipment_id="SHP1006", driver_quote="cancel"), say_text("x")])
    chat.say("yes", [say_text("ok")])
    assert "confirm_reschedule" not in chat.tools_offered() and "cancel_appointment" in chat.tools_offered()


def test_a_cancellation_that_needs_a_person_is_handed_off_at_once(booked: Chat) -> None:
    booked.clock.set(at("11:30"))
    r = booked.say("cancel SHP1017 please", _cancel("cancel SHP1017"))
    assert r.status == "HANDOFF" and r.escalation_reason == "CANCELLATION_CUTOFF" and _status(booked) == "CONFIRMED"
