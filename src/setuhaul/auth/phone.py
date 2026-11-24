"""Phone numbers in one canonical form (E.164), so the same number always compares equal."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import re

# Separators people type inside a number.
_SEPARATORS = str.maketrans("", "", " \t-().")
# Canonical form: plus, a non-zero digit, then 9 to 14 more.
_E164 = re.compile(r"\+[1-9][0-9]{9,14}")


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
        # A trunk 0 typed after the country code is not part of the number: +91 0 9000010006.
        if digits.startswith(cc + "0") and len(digits) == len(cc) + 11:
            digits = cc + digits[len(cc) + 1:]
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
