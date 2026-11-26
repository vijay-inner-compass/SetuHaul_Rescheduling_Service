"""Reading order numbers out of what a driver types."""

from __future__ import annotations

import pytest

from setuhaul.order_numbers import is_valid, mentions, normalise


@pytest.mark.parametrize("text", ["ORD-261127-001", "ord 261127 001", "my order is Ord261127001.", "ORD-261127-001 5 pm",
                                  "ऑर्डर ORD-261127-001 है"])
def test_finds_the_number(text: str) -> None:
    assert any("ORD261127001" in m for m in mentions(text))


@pytest.mark.parametrize("text", ["261127001", "call +919811100001", "ETA 1730", "ORD-12345", "WORD261127001", "ORD261127001X",
                                  "ORD-२६११२७-००१"])
def test_not_a_mention(text: str) -> None:
    assert mentions(text) == []


def test_two_numbers_are_two_mentions() -> None:
    assert len(mentions("ORD-261127-001 and ORD-261127-002")) == 2


def test_a_shorter_number_does_not_match_a_longer_hyphenated_one() -> None:
    assert not any("ORD261127" in m for m in mentions("ORD-261127-001"))


def test_validity_and_normalising() -> None:
    assert normalise("ord 261127-001") == "ORD261127001"
    assert is_valid("ORD-261127-001") and is_valid("ord 261127 001")
    for bad in ("ORD-DEMO-P1", "ORD-261127-001 ok", "ORD-12345", "261127001", "ORD_261127001", ""):
        assert not is_valid(bad), bad
