"""The driver asks in chat to change facility; the reply says it is only a request."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_auto_hold import Chat


@pytest.fixture()
def chat(tmp_path: Path) -> Chat:
    return Chat(tmp_path)


def _requests(chat: Chat) -> list[tuple[str, str]]:
    return [(r["to_facility_id"], r["status"]) for r in chat.rows("select to_facility_id, status from facility_change_requests")]


def test_change_to_gurugram_creates_a_pending_request(chat: Chat) -> None:
    reply = chat.say("please change facility to Gurugram")
    assert _requests(chat) == [("FAC-GGN-01", "PENDING")]
    # Only a request, naming both ends; nothing is booked or moved.
    assert "Gurugram" in reply and "Jaipur" in reply and "not changed" in reply.lower()
    assert chat.rows("select destination_facility_id from shipments where shipment_id='SHP1006'")[0][0] == "FAC-JAI-01"


def test_booking_is_untouched_while_pending(chat: Chat) -> None:
    original = chat.current()
    chat.say("please change facility to Gurugram")
    assert chat.current() == original and chat.held() == []


def test_a_second_ask_reports_the_pending_one(chat: Chat) -> None:
    chat.say("please change facility to Gurugram")
    reply = chat.say("please change facility to Gurugram")
    assert len(_requests(chat)) == 1 and "already waiting" in reply.lower()
