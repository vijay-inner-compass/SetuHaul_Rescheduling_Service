"""After approval: a slot is held at the new facility, or the load goes back where it was."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from setuhaul import facility_change as fc
from setuhaul.auth import users
from setuhaul.db import open_connection
from tests.test_auto_hold import Chat

GGN, JAI, PARTNER = "FAC-GGN-01", "FAC-JAI-01", "FAC-PARTNER-01"


@pytest.fixture()
def chat(tmp_path: Path) -> Chat:
    return Chat(tmp_path)


def _approve(chat: Chat, to_words: str) -> tuple[str, Any]:
    chat.say(f"please change facility to {to_words}")
    conn = open_connection(path=chat.deps.db_path)
    admin = users.bootstrap_admin(conn, "boss@x.com", chat.deps.clock)
    rid = conn.execute("select request_id from facility_change_requests where status='PENDING'").fetchone()[0]
    assert fc.decide(conn, chat.deps.clock, actor=admin, request_id=rid, approve=True).status == "APPROVED"
    outcome = fc.after_approval(conn, chat.deps.redis, chat.deps.clock, rid)
    return outcome, conn


def _dest(chat: Chat) -> str:
    return str(chat.rows("select destination_facility_id from shipments where shipment_id='SHP1006'")[0][0])


def _last_message(chat: Chat) -> str:
    return str(chat.rows("select message_text from chat_messages order by rowid desc limit 1")[0][0])


def test_confirm_across_facilities_ends_the_old_one_atomically(chat: Chat) -> None:
    old = chat.current()
    outcome, _ = _approve(chat, "Gurugram")
    # Approving frees the old slot immediately and holds a slot at Gurugram.
    assert outcome == "HELD"
    assert tuple(chat.rows(f"select appointment_status, is_current from appointments where slot_id='{old}'")[0]) == ("CANCELLED", 0)
    assert chat.rows("select count(*) from appointments where shipment_id='SHP1006' and is_current=1")[0][0] == 0
    reply = chat.say("Yes, confirm")
    assert reply.startswith("Your appointment is confirmed") and "Gurugram" in reply
    assert chat.current().startswith("SLOT-GGN")
    live = chat.rows("select count(*) from appointments where shipment_id='SHP1006' and is_current=1 and appointment_status='CONFIRMED'")
    assert live[0][0] == 1


def test_approval_holds_at_the_new_facility_long_enough_to_be_seen_and_tells_the_driver(chat: Chat) -> None:
    outcome, _ = _approve(chat, "Gurugram")
    assert outcome == "HELD" and "Gurugram" in _last_message(chat) and "Yes" in _last_message(chat)
    chat.deps.clock.advance(timedelta(minutes=10))
    assert chat.say("Yes, confirm").startswith("Your appointment is confirmed")


def test_no_slot_there_sends_the_load_back_and_opens_a_case(chat: Chat) -> None:
    old = chat.current()
    conn = open_connection(path=chat.deps.db_path)
    conn.execute("update appointment_slots set slot_status='BLOCKED' where dock_id in (select dock_id from docks where facility_id=?)", (GGN,))
    conn.commit()
    conn.close()
    outcome, _ = _approve(chat, "Gurugram")
    assert outcome == "NO_SLOT" and _dest(chat) == GGN and chat.held() == []
    assert tuple(chat.rows(f"select appointment_status, is_current from appointments where slot_id='{old}'")[0]) == ("CANCELLED", 0)
    row = chat.rows("select status, reason from facility_change_requests")[0]
    assert tuple(row) == ("APPROVED", "NO_SLOT") and "freed" in _last_message(chat)
    assert chat.rows("select count(*) from escalation_cases where shipment_id='SHP1006'")[0][0] >= 1


def test_partner_destination_sends_a_request_and_never_says_booked(chat: Chat) -> None:
    outcome, _ = _approve(chat, "the partner")
    assert outcome == "PARTNER_REQUESTED" and chat.held() == []
    assert chat.rows("select count(*) from partner_requests where status='PENDING_PARTNER'")[0][0] == 1
    assert "not booked" in _last_message(chat).lower()


def test_a_partner_move_drops_the_hold_at_the_old_facility_so_yes_cannot_book_it(chat: Chat) -> None:
    old = chat.current()
    chat.say("I will be about two hours late")
    assert len(chat.held()) == 1
    outcome, _ = _approve(chat, "the partner")
    assert outcome == "PARTNER_REQUESTED" and chat.held() == []
    assert not chat.say("Yes, confirm").startswith("Your appointment is confirmed")
    assert tuple(chat.rows(f"select appointment_status, is_current from appointments where slot_id='{old}'")[0]) == ("CANCELLED", 0)


def test_the_driver_card_names_the_warehouse_of_the_old_booking_after_a_move(tmp_path: Path) -> None:
    chat = Chat(tmp_path)
    _approve(chat, "Gurugram")
    card = chat.client.get("/demo/driver/api/state").json()["booking"]
    assert "Gurugram" in card["facility"] and card["booked"] is None
