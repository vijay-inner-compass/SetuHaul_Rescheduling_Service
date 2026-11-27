"""Every refusal a screen can meet is a sentence, wherever the screen shows it (UX audit 2026-11-27: UX-06)."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "setuhaul"
ASSETS = SRC / "auth" / "assets"
# Raised only for machine callers (webhooks, sign-in redirects) or turned into a page before anyone reads a code.
NOT_ON_SCREEN = {"BAD_TOKEN", "BIND_FAILED", "GOOGLE_NOT_CONFIGURED", "INVALID_SECRET"}
_RAISED = re.compile(r'(?:AuthError|AuthzError)\((?:\d{3}, )?"([A-Z_]{4,})"|reject\(\w+, \d+, "([A-Z_]+)"|"reason_code": "([A-Z_]+)"')


def _script(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def _table(text: str, opening: str, closing: str) -> set[str]:
    found = re.search(rf"{opening}(.*?){closing}", text, re.DOTALL)
    assert found, opening
    return set(re.findall(r'^\s+"?([A-Z_]{4,})"?: ', found.group(1), re.MULTILINE))


def test_every_code_a_screen_can_meet_has_a_sentence() -> None:
    raised: set[str] = set()
    for path in [*(SRC / "auth").glob("*.py"), SRC / "passes.py"]:
        raised |= {next(filter(None, m)) for m in _RAISED.findall(path.read_text(encoding="utf-8"))}
    known = _table(_script("common.js"), r"var MESSAGES = \{", r"\n  \};")
    for path in (SRC / "auth").glob("*_routes.py"):
        text = path.read_text(encoding="utf-8")
        if "_MESSAGES = {" in text:
            known |= _table(text, r"_MESSAGES = \{", r"\n\}")
    assert raised - known - NOT_ON_SCREEN == set()


def test_other_screen_scripts_use_the_shared_sentences() -> None:
    for name in ("settings.js", "gate.js", "warehouse.js"):
        text = _script(name)
        assert "window.SH.reason(" in text, name
        assert "data.reason_code ||" not in text and "data.message || data.reason_code" not in text, name
    assert "reason: reason" in _script("common.js")


def test_a_refused_field_is_marked_and_linked_to_its_message() -> None:
    text = _script("common.js")
    assert 'setAttribute("aria-invalid", "true")' in text and 'setAttribute("aria-describedby"' in text
    for code, field in (("INVALID_PHONE", "phone"), ("INVALID_EMAIL", "email"), ("DUPLICATE_ORDER_NUMBER", "order_number")):
        assert re.search(rf'{code}: "{field}"', text)
