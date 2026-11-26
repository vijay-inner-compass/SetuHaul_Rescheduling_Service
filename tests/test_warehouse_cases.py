"""Working a hand-off case from the Warehouse screen (UX audit 2026-11-27: UX-01, 03, 06, 22)."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pytest

from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from setuhaul.escalation import RESOLUTION_CODES
from tests.auth_helpers import GURUGRAM, JAIPUR, World, build_world

SRC = Path(__file__).resolve().parents[1] / "src" / "setuhaul"
CASE = f"ESC-T-{JAIPUR}"


@pytest.fixture()
def world(tmp_path: Path, clock: FakeClock) -> World:
    return build_world(tmp_path, clock)


def _page(world: World, who: str) -> str:
    r = world.client(who).get("/warehouse")
    assert r.status_code == 200
    return str(r.text)


def _card(page: str, case_id: str = CASE) -> str:
    # The one list item for this case, or "" when it is not on the page.
    found = re.search(rf'<li class="case[^"]*" data-key="case:{case_id}".*?</li><!--case-->', page, re.DOTALL)
    return found.group(0) if found else ""


def _post(world: World, who: str, step: str, body: dict[str, str] | None = None, case_id: str = CASE):  # type: ignore[no-untyped-def]
    return world.client(who).post(f"/v1/coordinator/cases/{case_id}/{step}", json=body or {})


def test_open_case_offers_claim_only_within_scope(world: World) -> None:
    page = _page(world, "wh_a")
    card = _card(page)
    assert f'data-api="/v1/coordinator/cases/{CASE}/claim"' in card and ">Claim</button>" in card
    assert "/resolve" not in card
    assert not _card(page, f"ESC-T-{GURUGRAM}")


def test_claimant_gets_resolve_form_and_others_see_the_owner(world: World) -> None:
    assert _post(world, "wh_a", "claim").status_code == 200
    mine = _card(_page(world, "wh_a"))
    assert f'data-api="/v1/coordinator/cases/{CASE}/resolve"' in mine and "data-confirm=" in mine
    assert set(re.findall(r'<option value="([A-Z_]+)"', mine)) == RESOLUTION_CODES
    # No outcome is preselected and the message starts empty: nothing is sent that the user did not choose or write.
    assert '<option value="" selected disabled>' in mine
    box = re.search(r"<textarea ([^>]*)>(.*?)</textarea>", mine, re.DOTALL)
    assert box and 'name="note"' in box.group(1) and "required" in box.group(1) and 'maxlength="500"' in box.group(1)
    assert box.group(2) == ""
    theirs = _card(_page(world, "admin"))
    assert "Claimed by Wha" in theirs and "data-api=" not in theirs and "<textarea" not in theirs
    # The server refuses the second claimant and names the owner for the message.
    again = _post(world, "admin", "claim")
    assert again.status_code == 409 and again.json()["claimed_by"] == "web:wha@x.com"


def test_resolve_sends_the_typed_note_and_offers_reopen(world: World) -> None:
    _post(world, "wh_a", "claim")
    note = "Dock 3 is yours at <14:00>."
    r = _post(world, "wh_a", "resolve", {"resolution_code": "ADVISED_DRIVER", "note": note})
    assert r.status_code == 200
    conn = open_connection(path=world.path, readonly=True)
    sent = [m["message_text"] for m in conn.execute("select message_text from chat_messages where thread_id=?", (f"THR-T-{JAIPUR}",))]
    conn.close()
    assert any(m.endswith(note) for m in sent)
    page = _page(world, "wh_a")
    card = _card(page)
    assert "Recently resolved" in page and f'data-api="/v1/coordinator/cases/{CASE}/reopen"' in card
    assert "/claim" not in card and "<textarea" not in card
    # The typed note is escaped wherever it is shown.
    assert note not in page and "&lt;14:00&gt;" in card
    assert 'data-count="0"' in page


def test_case_resolved_before_the_reopen_window_is_not_listed(world: World) -> None:
    _post(world, "wh_a", "claim")
    _post(world, "wh_a", "resolve", {"resolution_code": "OTHER", "note": "done"})
    old = (world.clock.now() - timedelta(hours=25)).isoformat()
    conn = open_connection(path=world.path)
    conn.execute("update escalation_cases set resolved_at=? where case_id=?", (old, CASE))
    conn.commit()
    conn.close()
    assert not _card(_page(world, "wh_a"))


def test_case_card_shows_its_history_with_people_not_identities(world: World) -> None:
    _post(world, "wh_a", "claim")
    card = _card(_page(world, "admin"))
    assert "History" in card and "Claimed" in card and "by Wha" in card and "web:wha@x.com" not in card
    # The folded heading can be focused, so it needs its own key for the live refresh to give focus back.
    assert f'<summary data-key="history-toggle:{CASE}">' in card


def test_actions_carry_a_success_message(world: World) -> None:
    assert 'data-done="Case claimed."' in _card(_page(world, "wh_a"))
    _post(world, "wh_a", "claim")
    assert 'data-done="Case resolved.' in _card(_page(world, "wh_a"))


def test_every_case_refusal_has_a_sentence() -> None:
    script = (SRC / "auth" / "assets" / "common.js").read_text(encoding="utf-8")
    table = re.search(r"var MESSAGES = \{(.*?)\n  \};", script, re.DOTALL)
    assert table
    known = set(re.findall(r"^    ([A-Z_]+): ", table.group(1), re.MULTILINE))
    needed = {"NOT_FOUND", "ALREADY_CLAIMED", "NOT_OPEN", "NOT_CLAIMED", "NOT_YOUR_CASE", "ALREADY_FINISHED", "NOT_RESOLVED",
              "REOPEN_WINDOW_PASSED", "INVALID_RESOLUTION", "INVALID_REQUEST", "DATABASE_UNAVAILABLE", "CROSS_SITE_REQUEST",
              "UNAUTHORIZED", "UNAUTHENTICATED", "FORBIDDEN", "FACILITY_REQUIRED", "INVALID_PHONE", "BAD_CREDENTIALS"}
    assert needed <= known, needed - known


def test_demo_resolve_message_starts_empty() -> None:
    page = (SRC / "demo" / "index.html").read_text(encoding="utf-8")
    box = re.search(r'<textarea id="rn"([^>]*)>(.*?)</textarea>', page, re.DOTALL)
    assert box and "required" in box.group(1) and box.group(2) == ""
    assert "We have booked" not in page
