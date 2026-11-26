"""The pass code itself: shape, finding it however it is typed, and blanking it."""

from __future__ import annotations

import re

import pytest

from setuhaul.dock_pass import blank, find, hash_code, new_code, normalise


def test_code_shape_and_randomness() -> None:
    code = new_code()
    assert re.fullmatch(r"DAP-[A-Z2-7]{5}-[A-Z2-7]{5}-[A-Z2-7]{5}-[A-Z2-7]{5}-[A-Z2-7]{6}", code)
    assert len({new_code() for _ in range(1000)}) == 1000
    assert hash_code(code) == hash_code(code.lower().replace("-", " ")) != hash_code(new_code())


@pytest.mark.parametrize("shape", [
    lambda c: c, lambda c: c.lower(), lambda c: c.replace("-", " "), lambda c: f"my pass {c}.", lambda c: c[4:].replace("-", ""),
    lambda c: c[:-1], lambda c: c + "A", lambda c: c.replace("-", ""), lambda c: c[:12],
    lambda c: f"Your Dock Access Pass for order ORD-261127-001:\n{c}\nSend it back here",
])
def test_found_and_blanked_however_typed(shape) -> None:  # type: ignore[no-untyped-def]
    code = new_code()
    text = shape(code)
    secret = normalise(code)[3:11]
    assert find(text) and secret not in normalise(blank(text))


def test_a_whole_code_is_found_exactly() -> None:
    code = new_code()
    assert find(f"here: {code} thanks") == [normalise(code)]
    assert find(code[4:].replace("-", "")) == [normalise(code)]


@pytest.mark.parametrize("text", ["I will dap in at 5", "ORD-261127-001", "call +919811100001", "dapper driver", "DAP price is high",
                                  "my truck broke down near the highway"])
def test_ordinary_words_are_left_alone(text: str) -> None:
    assert find(text) == [] and blank(text) == text
