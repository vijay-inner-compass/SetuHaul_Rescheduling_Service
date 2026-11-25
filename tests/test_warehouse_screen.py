"""The warehouse screen: what needs attention, what is on each bay now, and messages that stay readable when long."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from datetime import datetime
from typing import Any

from setuhaul.auth.pages import _board_section, _case_cards, _chat_browser

NOW = datetime.fromisoformat("2026-08-04T10:15:00+05:30")


def _slot(hour: int, status: str, label: str = "", past: bool = False) -> dict[str, Any]:
    return {"hour": hour, "status": status, "label": label, "past": past, "start": f"{hour:02d}:00", "end": f"{hour + 1:02d}:00"}


BOARD = {"facility": "FAC-JAI-01", "hours": [9, 10, 11], "pending_partner": [], "docks": [
    {"code": "D1", "type": "Standard", "slots": [_slot(9, "BOOKED", "Asha Rao · ORD-1", True), _slot(10, "BOOKED", "Ravi <b> · ORD-2"),
                                                 _slot(11, "AVAILABLE")]},
    {"code": "D2", "type": "Reefer", "slots": [_slot(10, "AVAILABLE"), _slot(11, "HELD", "Manoj Sharma · ORD-3")]}]}


def test_board_shows_counts_what_is_on_each_bay_and_the_current_hour() -> None:
    page = _board_section(BOARD, [("FAC-JAI-01", "Jaipur")], "FAC-JAI-01", NOW)
    assert "On the bays now" in page and 'class="now"' in page
    # D1 is working Ravi now; D2 is free now with Manoj next at 11:00.
    assert "ORD-2" in page and "Next 11:00" in page and "Manoj Sharma" in page
    assert "cell-booked" in page and "cell-held" in page and 'class="legend-key' in page
    # Names are escaped.
    assert "Ravi &lt;b&gt;" in page and "Ravi <b>" not in page


def test_long_chats_are_one_list_and_one_scrolling_conversation_each() -> None:
    long = "word " * 400
    chats = [{"driver_id": "DRV006", "driver_name": "Manoj", "messages": [
        {"role": "DRIVER", "text": long, "timestamp": "2026-08-04T09:35:00+05:30"},
        {"role": "AGENT", "text": "<script>x</script>", "timestamp": "2026-08-04T09:36:00+05:30"}]},
             {"driver_id": "DRV007", "driver_name": "Nitin", "messages": [
                 {"role": "DRIVER", "text": "ok", "timestamp": "2026-08-04T09:00:00+05:30"}]}]
    page = _chat_browser(chats)
    assert page.count('data-thread="') == 2 and page.count('class="thread"') == 2
    # The list shows a short preview; the whole text is only in the conversation.
    preview = page.split('class="preview">', 1)[1].split("<", 1)[0]
    assert len(preview) <= 91
    assert long.strip() in page and "<script>" not in page and "09:36" in page


def test_long_case_notes_are_folded() -> None:
    page = _case_cards([{"id": "CAS1", "status": "OPEN", "reason": "NO_SLOT", "summary": "late " * 100, "load": "SHP1", "claimed": ""}])
    assert "<details" in page and "CAS1" in page
    assert "No open cases." in _case_cards([])
