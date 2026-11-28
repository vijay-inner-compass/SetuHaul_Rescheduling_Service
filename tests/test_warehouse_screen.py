"""The warehouse screen: what needs attention, what is on each bay now, and messages that stay readable when long."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from setuhaul.auth.pages import _bay_card, _board_section, _case_cards, _chat_browser
from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from setuhaul.escalation import get_case
from tests.auth_helpers import JAIPUR, build_world

NOW = datetime.fromisoformat("2026-08-04T10:15:00+05:30")


def _slot(hour: int, status: str, label: str = "", past: bool = False, minute: int = 0) -> dict[str, Any]:
    start, end = f"{hour:02d}:{minute:02d}", f"{hour + 1:02d}:{minute:02d}"
    return {"hour": hour, "status": status, "label": label, "past": past, "start": start, "end": end,
            "start_ts": f"2026-08-04T{start}:00+05:30", "end_ts": f"2026-08-04T{end}:00+05:30"}


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


def test_long_case_notes_are_folded(tmp_path: Path, clock: FakeClock) -> None:
    world = build_world(tmp_path, clock)
    conn = open_connection(path=world.path)
    try:
        conn.execute("update escalation_cases set summary=? where case_id=?", ("late " * 100, world.cases[JAIPUR]))
        conn.commit()
        user = world.users["wh_a"]
        case = get_case(conn, world.cases[JAIPUR])
        assert case is not None
        page = _case_cards(conn, user, [case], "No open cases.", clock.now())
        assert 'class="note-fold"' in page and case.case_id in page
        assert "No open cases." in _case_cards(conn, user, [], "No open cases.", clock.now())
    finally:
        conn.close()


def test_a_booking_that_starts_mid_hour_is_judged_by_its_real_times() -> None:
    dock = {"code": "P1", "type": "Standard", "slots": [_slot(10, "BOOKED", "Asha Rao · ORD-9", minute=30)]}

    def card(clock: str) -> str:
        return _bay_card(dock, datetime.fromisoformat(f"2026-08-04T{clock}:00+05:30"))

    # Before it starts: nothing is on the bay, and the booking is what comes next.
    before = card("10:15")
    assert '<div class="occupant"></div>' in before and "Next 10:30 Asha Rao" in before
    # While it runs, including after the clock hour has changed: it is on the bay.
    for clock in ("10:30", "11:15"):
        during = card(clock)
        assert "ORD-9" in during and "Booked" in during and "Nothing else booked" in during
    # The end time is exclusive.
    assert "ORD-9" not in card("11:30")


# ---- compact board --------------------------------------------------------------------------------------------
def _one(slots: list[dict[str, Any]], code: str = "D1") -> str:
    board = {"facility": "FAC-JAI-01", "hours": sorted({s["hour"] for s in slots}), "pending_partner": [],
             "docks": [{"code": code, "type": "Standard", "slots": slots}]}
    page = _board_section(board, [("FAC-JAI-01", "FAC-JAI-01 - Jaipur")], "FAC-JAI-01", NOW)
    return page.split('<table class="board"', 1)[1].split("</table>", 1)[0]


def _buttons(table: str) -> list[tuple[str, str]]:
    return re.findall(r'<button type="button" class="slot"([^>]*)>([^<]*)</button>', table)


def test_cells_show_initials_and_carry_the_detail() -> None:
    [(attrs, text)] = _buttons(_one([_slot(11, "BOOKED", "Manoj Sharma · ORD-1")]))
    assert text == "MS"
    full = "D1 11:00-12:00: Booked, Manoj Sharma, ORD-1"
    assert f'aria-label="{full}"' in attrs and f'data-detail="{full}"' in attrs and f'title="{full}"' in attrs
    assert "data-free" not in attrs


def test_two_slots_in_one_hour_are_both_buttons() -> None:
    table = _one([_slot(10, "BOOKED", "Asha Rao · ORD-1"), _slot(10, "AVAILABLE", minute=30)])
    assert [text for _, text in _buttons(table)] == ["AR", ""]


def test_a_blocked_cell_with_a_booking_names_both() -> None:
    slot = _slot(11, "BLOCKED", "Asha Rao · ORD-1") | {"reason": "Leveller <broke>"}
    [(attrs, text)] = _buttons(_one([slot]))
    assert text == "AR" and "D1 11:00-12:00: Blocked, Asha Rao, ORD-1, Leveller &lt;broke&gt;" in attrs


def test_free_cells_have_no_visible_word_but_a_name() -> None:
    [(attrs, text)] = _buttons(_one([_slot(11, "AVAILABLE")]))
    assert text == "" and 'aria-label="D1 11:00-12:00: Free"' in attrs


def test_freeable_slot_carries_the_free_address_and_question() -> None:
    slot = _slot(11, "BOOKED", "Asha Rao · ORD-1") | {"freeable": True, "appointment_id": "APT 1"}
    [(attrs, _)] = _buttons(_one([slot]))
    assert 'data-free="/v1/warehouse/appointments/APT%201/free"' in attrs
    assert "Free D1 11:00 for ORD-1 (Asha Rao)? The shipment stays and will need a new slot." in attrs


def test_facilities_are_buttons_and_six_fall_back_to_a_list() -> None:
    few = [("FAC-A", "FAC-A - Alpha"), ("FAC-B", "FAC-B - Beta")]
    page = _board_section(BOARD, few, "FAC-B", NOW)
    assert '<a href="/warehouse?facility=FAC-B#board" aria-current="true">Beta</a>' in page
    assert 'data-facility="FAC-B"' in page and "<select name=\"facility\">" not in page
    many = [(f"FAC-{n}", f"FAC-{n} - Site {n}") for n in range(6)]
    page = _board_section(BOARD, many, "FAC-1", NOW)
    assert "<select name=\"facility\">" in page and 'data-facility="FAC-1"' in page


def test_the_detail_box_is_outside_what_the_refresh_replaces() -> None:
    page = _board_section(BOARD, [("FAC-JAI-01", "Jaipur")], "FAC-JAI-01", NOW)
    assert page.count('<dialog id="slot-detail"') == 1
    assert page.index('<dialog id="slot-detail"') > page.index("</table></div>")


# ---- operations panel -----------------------------------------------------------------------------------------
def _warehouse(tmp_path: Path, clock: FakeClock) -> str:
    world = build_world(tmp_path, clock)
    return world.client("wh_a").get("/warehouse").text


def test_the_board_tab_holds_the_operations_panel(tmp_path: Path, clock: FakeClock) -> None:
    page = _warehouse(tmp_path, clock)
    board = page.split('data-panel="board"', 1)[1].split('data-panel="projects"', 1)[0]
    assert '<aside id="messages" class="ops"' in board
    assert board.index('<table class="board"') < board.index('<aside id="messages"')


def test_tabs_are_four_and_there_is_no_messages_tab(tmp_path: Path, clock: FakeClock) -> None:
    page = _warehouse(tmp_path, clock)
    assert re.findall(r'data-tab="(\w+)"', page) == ["board", "projects", "shipments", "drivers"]
    assert 'data-panel="messages"' not in page


def test_each_view_has_its_content(tmp_path: Path, clock: FakeClock) -> None:
    page = _warehouse(tmp_path, clock)
    panel = page.split('<aside id="messages"', 1)[1].split("</aside>", 1)[0]
    assert re.findall(r'data-view="(\w+)"', panel) == ["cases", "chats", "requests"]
    views = {name: body for name, body in zip(re.findall(r'data-pane="(\w+)"', panel),
                                              re.split(r'data-pane="\w+"', panel)[1:], strict=True)}
    assert "ESC-T-FAC-JAI-01" in views["cases"] and "ESC-T-FAC-GGN-01" not in panel
    assert "hello from DRV001" in views["chats"]
    assert "PR-T-FAC-JAI-01" in views["requests"] and 'class="waiting"' in views["requests"]
    # One open case in scope is counted on its view.
    assert re.search(r'data-view="cases"[^>]*>Cases <span class="count">1</span>', panel)


def test_bays_and_blocking_are_folded_below_the_board(tmp_path: Path, clock: FakeClock) -> None:
    page = _warehouse(tmp_path, clock)
    assert '<details data-key="bays"' in page and '<details data-key="blocking"' in page
    assert page.index('<table class="board"') < page.index('<details data-key="bays"')
    assert page.index('id="block-result"') < page.index('<details data-key="blocking"')


def test_attention_cards_still_point_at_messages(tmp_path: Path, clock: FakeClock) -> None:
    page = _warehouse(tmp_path, clock)
    assert 'href="#messages"' in page
    script = (Path(__file__).resolve().parents[1] / "src/setuhaul/auth/assets/tabs.js").read_text(encoding="utf-8")
    assert '"messages"' in script


def test_board_always_shows_all_24_hours() -> None:
    page = _board_section(BOARD, [("FAC-JAI-01", "Jaipur")], "FAC-JAI-01", NOW)
    for hour in range(24):
        assert f">{hour:02d}:00</th>" in page
    assert "cell-none" in page
