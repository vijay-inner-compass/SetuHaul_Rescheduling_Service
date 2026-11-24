"""Phone numbers in one canonical form (E.164), so the same number always compares equal."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

# Separators people type inside a number.
_SEPARATORS = str.maketrans("", "", " \t-().")


def normalise_e164(raw: str, default_country: str = "91") -> str | None:
    # Drop spaces, hyphens, parentheses and dots.
    text = raw.translate(_SEPARATORS)
    # A leading plus means the country code is already there.
    international = text.startswith("+")
    digits = text[1:] if international else text
    # Anything but digits (letters, a stray plus) is not a number.
    if not digits.isascii() or not digits.isdigit():
        return None
    if not international:
        if len(digits) == 11 and digits.startswith("0"):
            # National trunk prefix: 0 + 10 digits.
            digits = default_country + digits[1:]
        elif len(digits) == 10:
            # A bare national number.
            digits = default_country + digits
        # Otherwise (e.g. 919000010006) the digits already carry the country code.
    # E.164 allows at most 15 digits; fewer than 10 cannot be a full number.
    if not 10 <= len(digits) <= 15:
        return None
    return "+" + digits
