"""The driver-chat tool that asks a partner warehouse for a slot."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from setuhaul.clock import FakeClock
from tests.conv_helpers import Chat, say_text, tc, tool_json
from tests.helpers import seed_everything_known, seed_partner_facility

QUOTE = "please ask the warehouse for 14:00"


@pytest.fixture()
def chat(db: sqlite3.Connection, clock: FakeClock, tmp_path: Path) -> Chat:
    seed_everything_known(db)
    return Chat(db, clock, tmp_path)


def _ask(chat: Chat, shipment: str = "SHP1006", start: str = "14:00", quote: str = QUOTE):  # type: ignore[no-untyped-def]
    seen: dict = {}  # type: ignore[type-arg]

    def grab(msgs):  # type: ignore[no-untyped-def]
        seen.update(tool_json(msgs, "request_partner_slot"))
        return say_text("All confirmed, see you at 14:00!")  # a false claim the output guard must not let through

    reply = chat.say(quote, [tc("request_partner_slot", shipment_id=shipment, start_time=start, driver_quote=quote), grab])
    return reply, seen


def test_tool_is_offered_only_when_a_load_is_bound_for_a_partner(chat: Chat) -> None:
    chat.say("hello", [say_text("hi")])
    assert "request_partner_slot" not in chat.tools_offered()
    seed_partner_facility(chat.db)
    chat.say("hello again", [say_text("hi")])
    assert "request_partner_slot" in chat.tools_offered()


def test_tool_requests_partner_slot_for_own_shipment(chat: Chat) -> None:
    seed_partner_facility(chat.db)
    reply, seen = _ask(chat)
    assert seen["status"] == "REQUESTED" and seen["request_ref"].startswith("PR-")
    row = chat.db.execute("select status, proposed_start from partner_requests").fetchone()
    assert row["status"] == "PENDING_PARTNER" and "T14:00:00" in row["proposed_start"]
    assert "request_partner_slot" in reply.tools_executed


def test_answer_is_rendered_from_stored_state_not_the_models_claim(chat: Chat) -> None:
    seed_partner_facility(chat.db)
    reply, _ = _ask(chat)
    assert "waiting for their answer" in reply.text
    assert "confirmed, see you" not in reply.text.lower()
    # nothing about the driver's real appointment changed
    assert chat.db.execute("select is_current from appointments where appointment_id='APT1006'").fetchone()[0] == 1


def test_tool_refuses_other_drivers_shipment(chat: Chat) -> None:
    seed_partner_facility(chat.db, "SHP1012")  # a different driver's load goes to the partner
    chat.db.execute("update shipments set destination_facility_id='FAC-PARTNER-01' where shipment_id='SHP1006'")
    chat.db.commit()
    _, seen = _ask(chat, shipment="SHP1012")
    assert seen.get("error") == "FOREIGN_SHIPMENT" or seen.get("status") == "FOREIGN_SHIPMENT"
    assert chat.db.execute("select count(*) from partner_requests").fetchone()[0] == 0


def test_tool_refuses_non_partner_facility_destination(chat: Chat) -> None:
    seed_partner_facility(chat.db)
    chat.db.execute("update shipments set destination_facility_id='FAC-JAI-01' where shipment_id='SHP1006'")
    chat.db.commit()
    reply = chat.say(QUOTE, [tc("request_partner_slot", shipment_id="SHP1006", start_time="14:00", driver_quote=QUOTE), say_text("ok")])
    assert chat.db.execute("select count(*) from partner_requests").fetchone()[0] == 0
    assert reply.status in ("REPLIED", "ESCALATED")


def test_tool_rejects_a_time_that_is_not_hh_mm(chat: Chat) -> None:
    seed_partner_facility(chat.db)
    chat.say(QUOTE, [tc("request_partner_slot", shipment_id="SHP1006", start_time="tomorrow", driver_quote=QUOTE), say_text("ok")])
    assert chat.db.execute("select count(*) from partner_requests").fetchone()[0] == 0


def test_second_ask_in_same_conversation_does_not_duplicate(chat: Chat) -> None:
    seed_partner_facility(chat.db)
    _ask(chat)
    _, seen = _ask(chat)
    assert seen["status"] == "ALREADY_PENDING"
    assert chat.db.execute("select count(*) from partner_requests").fetchone()[0] == 1
