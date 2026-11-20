"""Phase 8: English, Hindi and Hinglish; unsupported languages get one question, then a hand-off."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from setuhaul.clock import FakeClock
from setuhaul.language import (
    LANGS,
    LANGUAGE_PROMPT,
    T,
    claims_confirmed,
    claims_held,
    detect_language,
    parse_language_choice,
)
from tests.conv_helpers import Chat, facts_from, say_text, tc, tc_dyn, tool_json
from tests.helpers import seed_everything_known

LATE_60 = {"kind": "late_by", "minutes": 60}


@pytest.fixture()
def chat(db: sqlite3.Connection, clock: FakeClock, tmp_path: Path) -> Chat:
    seed_everything_known(db)
    return Chat(db, clock, tmp_path)


# --- detection ---------------------------------------------------------------------------------------
@pytest.mark.parametrize(("text", "lang"), [
    ("my truck is stuck in traffic, 60 minutes late", "en"),
    ("please keep my original slot", "en"),
    ("hello", "en"),
    ("मेरा ट्रक 60 मिनट लेट है", "hi"),
    ("गाड़ी खराब हो गई है", "hi"),
    ("मेरा truck late है", "hi"),  # Devanagari mixed with English words
    ("bhai gaadi kharab ho gayi hai, 90 minute lagenge", "hinglish"),
    ("kal subah pahunch jayega", "hinglish"),
    ("traffic hai 1 ghanta late", "hinglish"),
])
def test_supported_languages_are_detected(text: str, lang: str) -> None:
    assert detect_language(text).language == lang


@pytest.mark.parametrize("text", ["என் லாரி தாமதமாக உள்ளது", "شاحنتي متأخرة", "我的卡车晚了"])
def test_other_scripts_are_unsupported(text: str) -> None:
    assert detect_language(text).language == "unsupported"


def test_ambiguous_input_falls_back_to_the_established_language_or_is_unknown() -> None:
    assert detect_language("12").language == "unknown"
    assert detect_language("ok", previous=None).language in ("en", "unknown")
    assert detect_language("???", previous="hinglish").language == "hinglish"


def test_language_choice_answers() -> None:
    assert parse_language_choice("English") == "en"
    assert parse_language_choice(" हिंदी ") == "hi"
    assert parse_language_choice("Hinglish") == "hinglish"
    assert parse_language_choice("my truck is late") is None


# --- deterministic wording ---------------------------------------------------------------------------
def test_every_template_exists_in_all_three_languages_with_the_same_placeholders() -> None:
    for key, variants in T.items():
        assert set(variants) == set(LANGS), key
        fields = {lang: set(re.findall(r"{(\w+)}", text)) for lang, text in variants.items()}
        assert fields["en"] == fields["hi"] == fields["hinglish"], key
    assert "English" in LANGUAGE_PROMPT and "हिंदी" in LANGUAGE_PROMPT


@pytest.mark.parametrize("text", ["Your appointment is confirmed.", "I have booked it.", "Aapki appointment confirm ho gayi hai.",
                                  "आपकी अपॉइंटमेंट कन्फर्म हो गई है।", "booking book ho gaya"])
def test_confirmation_claims_are_recognised_in_every_language(text: str) -> None:
    assert claims_confirmed(text)


@pytest.mark.parametrize("text", ["Maine slot hold kar liya hai.", "मैंने स्लॉट होल्ड कर लिया है।", "I am holding the slot for you."])
def test_hold_claims_are_recognised_in_every_language(text: str) -> None:
    assert claims_held(text)


@pytest.mark.parametrize("text", ["Which shipment is late?", "Kaun si shipment late hai?", "कौन सी शिपमेंट लेट है?"])
def test_questions_are_not_mistaken_for_claims(text: str) -> None:
    assert not claims_confirmed(text) and not claims_held(text)


# --- unsupported language: one question, then escalate ------------------------------------------------
TAMIL = "என் லாரி தாமதமாக உள்ளது"


def test_unsupported_language_gets_one_choice_question_then_a_hand_off(chat: Chat) -> None:
    first = chat.say(TAMIL, [])  # no scripted model steps: the model must not be consulted
    assert first.status == "LANGUAGE_PROMPT" and first.text == LANGUAGE_PROMPT and chat.model_calls == 0
    second = chat.say("வணக்கம் உதவி தேவை", [])
    assert second.status == "HANDOFF" and second.escalation_reason == "LANGUAGE_UNSUPPORTED" and chat.model_calls == 0
    assert "ESC-0001" in second.text and chat.sink.requests[0].context["driver_text"] == "வணக்கம் உதவி தேவை"
    assert chat.rows("select message_text from chat_messages where thread_id='THR001' and sender_type='DRIVER' "
                     "order by rowid desc limit 2") == [("வணக்கம் உதவி தேவை",), (TAMIL,)]  # original text retained


def test_answering_the_language_question_continues_in_that_language(chat: Chat) -> None:
    chat.say(TAMIL, [])
    ack = chat.say("Hindi", [])
    assert ack.status == "REPLIED" and ack.language == "hi" and "धन्यवाद" in ack.text
    assert chat.memory.load("DRV006", "THR001").state.language == "hi"
    nxt = chat.say("ठीक है", [say_text("कृपया बताइए कौन सी शिपमेंट लेट है।")])
    assert nxt.language == "hi" and nxt.text == "कृपया बताइए कौन सी शिपमेंट लेट है।"


# --- transcripts ----------------------------------------------------------------------------------------
def options_text(msgs):  # type: ignore[no-untyped-def]
    o = tool_json(msgs, "search_slot_options")["options"]
    return "; ".join(f"{x['rank']}) {x['start']} D{x['dock'][-1]}" for x in o)


def test_hindi_conversation_end_to_end(chat: Chat) -> None:
    r1 = chat.say("मेरा ट्रक 60 मिनट लेट है, शिपमेंट SHP1006", [
        tc("search_slot_options", shipment_id="SHP1006", report=LATE_60),
        lambda m: say_text("ये विकल्प हैं (अभी कोई होल्ड नहीं है): " + options_text(m))])
    assert r1.language == "hi" and r1.text.startswith("ये विकल्प हैं")
    r2 = chat.say("पहला विकल्प ठीक है", [
        tc_dyn("request_hold", lambda m: {"option_set_id": facts_from(m)["latest_option_set_id"], "rank": 1,
                                          "driver_quote": "पहला विकल्प"}), say_text("ठीक है")])
    assert r2.text == "मैंने आपके लिए 04 Aug 2026 को डॉक D2 पर 12:00-13:00 का स्लॉट 09:38 तक होल्ड किया है। " \
                      "इसे कन्फर्म करने के लिए जवाब दें, या कोई और विकल्प बताइए।"
    r3 = chat.say("कन्फर्म करें", [
        tc_dyn("confirm_reschedule", lambda m: {"hold_appointment_id": facts_from(m)["live_hold"]["appointment_id"],
                                                "driver_quote": "कन्फर्म करें"}), say_text("हो गया")])
    assert r3.language == "hi" and r3.text.startswith("आपकी अपॉइंटमेंट कन्फर्म हो गई है: डॉक D2, 04 Aug 2026 को 12:00-13:00")
    assert chat.rows("select message_text from chat_messages where sender_type='DRIVER' and message_text like 'मेरा ट्रक%'") == [
        ("मेरा ट्रक 60 मिनट लेट है, शिपमेंट SHP1006",)]


def test_hinglish_conversation_with_clarification_then_options(chat: Chat) -> None:
    ask = lambda m: say_text("Repair ke baad gaadi ko pahunchne mein kitna time lagega?")
    r1 = chat.say("bhai gaadi kharab ho gayi hai, 90 minute lagenge repair mein", [
        tc("compute_eta", shipment_id="SHP1006", report={"kind": "repair", "minutes": 90}), ask])
    assert r1.language == "hinglish" and tool_json(chat.last_messages(), "compute_eta")["code"] == "MISSING_TRAVEL_TIME"
    r2 = chat.say("phir 45 minute ka drive hai", [
        tc("search_slot_options", shipment_id="SHP1006", report={"kind": "repair", "minutes": 90, "travel_minutes": 45}),
        lambda m: say_text("Ye options hain (abhi koi hold nahi hai): " + options_text(m))])
    assert r2.language == "hinglish" and chat.held() == 0
    r3 = chat.say("pehla option theek hai", [
        tc_dyn("request_hold", lambda m: {"option_set_id": facts_from(m)["latest_option_set_id"], "rank": 1,
                                          "driver_quote": "pehla option"}), say_text("ok")])
    assert r3.text.startswith("Maine aapke liye 04 Aug 2026 ko dock") and "tak hold kiya hai" in r3.text


def test_hinglish_hold_expiry_notice_is_in_hinglish(chat: Chat) -> None:
    from datetime import timedelta

    chat.say("SHP1006 60 minute late hai", [tc("search_slot_options", shipment_id="SHP1006", report=LATE_60), say_text("options")])
    chat.say("pehla option theek hai", [tc_dyn("request_hold", lambda m: {"option_set_id": facts_from(m)["latest_option_set_id"],
                                                                        "rank": 1, "driver_quote": "pehla option"}), say_text("ok")])
    chat.clock.advance(timedelta(seconds=181))
    r = chat.say("haan confirm kar do", [say_text("Naye options dekhte hain.")])
    assert r.text.startswith("Us slot par aapka hold khatam ho gaya hai.") and "Naye options" in r.text


def test_reply_language_follows_the_driver_when_they_switch(chat: Chat) -> None:
    a = chat.say("which of my shipments are late?", [say_text("Which shipment do you mean?")])
    b = chat.say("मेरी गाड़ी लेट है", [say_text("कौन सी शिपमेंट?")])
    assert (a.language, b.language) == ("en", "hi")


@pytest.mark.parametrize("text", ["Option 1 was booked by someone else.", "That slot was booked by another driver.",
                                  "Option 2 is booked for a different truck."])
def test_describing_someone_elses_booking_is_not_a_claim_about_this_drivers(text: str) -> None:
    assert not claims_confirmed(text)


@pytest.mark.parametrize("text", ["Your slot is booked.", "It was booked for you.", "You are now booked on dock D2."])
def test_claims_about_this_drivers_booking_are_still_caught(text: str) -> None:
    assert claims_confirmed(text)


@pytest.mark.parametrize("text", ["Nothing has been booked or held by this message.", "I have not confirmed or held anything yet.",
                                  "Your slot is not confirmed.", "No appointment has been booked."])
def test_denials_are_not_mistaken_for_claims(text: str) -> None:
    assert not claims_confirmed(text) and not claims_held(text)


def test_a_denial_does_not_hide_a_real_claim_in_the_same_reply() -> None:
    assert claims_confirmed("Nothing was held earlier. Your appointment is now confirmed.")
    assert claims_held("I have not changed anything else. I am holding the 12:00 slot for you.")
