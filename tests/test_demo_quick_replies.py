"""The demo console's quick messages must match the one-slot flow: there is no list to pick from."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "src" / "setuhaul" / "demo" / "index.html"


def test_no_quick_message_picks_from_a_list_in_any_language() -> None:
    page = PAGE.read_text(encoding="utf-8")
    quick = page[page.index("const QUICK="):page.index("const STATUS_CHIP=")]
    for wording in ("first option", "pehla option", "पहला विकल्प"):
        assert wording not in quick.lower()
    # Confirming the held slot stays one tap away in each language.
    for confirm in ("yes, please confirm", "haan, confirm kar do", "हाँ, कन्फर्म करें"):
        assert confirm in quick
