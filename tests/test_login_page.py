"""The sign-in page's structure: what is offered, how errors are shown, and that it stays within the content policy."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import re

from setuhaul.auth.pages import _landing


def test_page_has_one_heading_a_status_line_and_the_invitation_note() -> None:
    page = _landing(True, True)
    assert len(re.findall(r"<h1\b", page)) == 1
    assert 'id="status"' in page and 'role="alert"' in page
    assert "Access is by invitation" in page and "Telegram" in page


def test_password_form_is_folded_away_only_when_google_is_the_main_way_in() -> None:
    assert re.search(r'<details class="backup">', _landing(True, True))
    assert re.search(r'<details class="backup" open>', _landing(False, True))
    assert "<details" not in _landing(True, False)


def test_password_form_is_friendly_to_password_managers_and_can_reveal() -> None:
    page = _landing(False, True)
    assert 'autocomplete="username"' in page and 'autocomplete="current-password"' in page
    assert 'data-reveal="password"' in page and 'data-api="/auth/local-admin"' in page


def test_google_button_carries_the_mark_and_nothing_is_inline() -> None:
    page = _landing(True, True)
    assert 'href="/auth/login"' in page and "Sign in with Google" in page and "<svg" in page
    assert re.search(r"\sstyle\s*=|<style|\son[a-z]+\s*=|<script(?![^>]*\bsrc=)", page) is None
    # Nothing is loaded from another site.
    assert re.search(r'(src|href)="https?://', page) is None
