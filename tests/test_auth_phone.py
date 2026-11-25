"""Phone normalisation to E.164."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import pytest

from setuhaul.auth.phone import normalise_e164


@pytest.mark.parametrize("raw", ["+91 90000 10006", "919000010006", "09000010006", "9000010006",
                                 "+91-9000010006", "(+91) 90000.10006", " +919000010006 "])
def test_indian_forms_normalise(raw: str) -> None:
    assert normalise_e164(raw) == "+919000010006"


@pytest.mark.parametrize("raw", ["", "   ", "abc", "90000x10006", "123456789", "+1234567890123456", "+91-CALL-ME"])
def test_bad_input_is_none(raw: str) -> None:
    assert normalise_e164(raw) is None


def test_other_country_with_plus_is_kept() -> None:
    assert normalise_e164("+44 20 7946 0958") == "+442079460958"


@pytest.mark.parametrize("raw", ["0091 9000010006", "00919000010006", "0919000010006", "+91 09000010006"])
def test_prefix_variants_collapse_to_canonical(raw: str) -> None:
    assert normalise_e164(raw) == "+919000010006"


@pytest.mark.parametrize("raw", ["+09000010006", "+0 9000010006", "0000000000000"])
def test_leading_zero_after_plus_is_none(raw: str) -> None:
    assert normalise_e164(raw) is None


# ---- hardening sweep (11c group C) ----

@pytest.mark.parametrize("raw", ["+91 009000010006", "+91 00 9000010006", "0091 009000010006"])
def test_second_trunk_zero_after_indian_code_is_none(raw: str) -> None:
    assert normalise_e164(raw) is None


@pytest.mark.parametrize("raw", ["+44 020 7946 0958", "+442079460958"[:3] + "0" + "2079460958", "+1 0 2025550123"])
def test_trunk_zero_after_other_country_code_is_none(raw: str) -> None:
    assert normalise_e164(raw) is None


def test_other_country_codes_with_inner_zeros_stay_valid() -> None:
    # Greece (+30) numbers legitimately start 2xxx; a zero deeper in the number is fine.
    assert normalise_e164("+30 210 1234567") == "+302101234567"
    assert normalise_e164("+44 20 7946 0958") == "+442079460958"
