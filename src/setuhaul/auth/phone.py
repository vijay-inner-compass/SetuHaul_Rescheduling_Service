"""Phone numbers in one canonical form (E.164), so the same number always compares equal."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import re

# Separators people type inside a number.
_SEPARATORS = str.maketrans("", "", " \t-().")
# Canonical form: plus, a non-zero digit, then 9 to 14 more.
_E164 = re.compile(r"\+[1-9][0-9]{9,14}")


# Two-digit ITU calling codes (one-digit codes are 1 and 7; every other prefix is treated as a three-digit code).
_TWO_DIGIT_CODES = frozenset({
    "20", "27", "30", "31", "32", "33", "34", "36", "39", "40", "41", "43", "44", "45", "46", "47", "48", "49",
    "51", "52", "53", "54", "55", "56", "57", "58", "60", "61", "62", "63", "64", "65", "66", "81", "82", "84",
    "86", "90", "91", "92", "93", "94", "95", "98",
})


def _code_length(digits: str) -> int:
    # How many leading digits are the country code.
    if digits[0] in "17":
        return 1
    return 2 if digits[:2] in _TWO_DIGIT_CODES else 3


def normalise_e164(raw: str, default_country: str = "91") -> str | None:
    # Drop spaces, hyphens, parentheses and dots.
    text = raw.translate(_SEPARATORS)
    # The international call prefix 00 means the same as a plus.
    if text.startswith("00"):
        text = "+" + text[2:]
    # A leading plus means the country code is already there.
    international = text.startswith("+")
    digits = text[1:] if international else text
    # Anything but digits (letters, a stray plus) is not a number.
    if not digits.isascii() or not digits.isdigit():
        return None
    cc = default_country
    if international:
        # No country code starts with 0.
        if digits[:1] == "0":
            return None
        # A 0 straight after the country code is a trunk prefix, not part of the number.
        code = digits[:_code_length(digits)]
        rest = digits[len(code):]
        if rest.startswith("0"):
            # The default country forgives one trunk 0 (+91 0 9000010006); a second one is an error.
            if code != cc or rest[1:].startswith("0"):
                return None
            digits = code + rest[1:]
    elif len(digits) == 10:
        # A bare national number.
        digits = cc + digits
    elif len(digits) == 11 and digits.startswith("0"):
        # National trunk prefix: 0 + 10 digits.
        digits = cc + digits[1:]
    elif len(digits) == len(cc) + 11 and digits.startswith("0" + cc):
        # Trunk 0 in front of a country-coded number.
        digits = digits[1:]
    # Otherwise (e.g. 919000010006) the digits already carry the country code.
    result = "+" + digits
    # E.164: a non-zero first digit and 10 to 15 digits in all.
    return result if _E164.fullmatch(result) else None
