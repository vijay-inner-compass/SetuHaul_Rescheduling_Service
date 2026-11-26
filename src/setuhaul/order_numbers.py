"""Order numbers as typed: ORD followed by at least six digits, with spaces or hyphens allowed. Pure functions."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import re

# A mention inside free text. Hyphen-joined digits are one unit; spaces separate units. ASCII digits only.
_MENTION = re.compile(r"(?<![A-Za-z0-9])ORD[ \t-]*([0-9]+(?:-[0-9]+)*(?:[ \t]+[0-9]+(?:-[0-9]+)*)*)(?![A-Za-z0-9])", re.IGNORECASE)
# A whole value as the warehouse enters it.
_WHOLE = re.compile(r"ORD[ -]*[0-9](?:[ -]*[0-9]){5,}", re.IGNORECASE)
# Fewer digits than this is not an order number.
MIN_DIGITS = 6


def normalise(ref: str) -> str:
    # Case, spaces and hyphens are formatting, not identity.
    return re.sub(r"[\s-]", "", ref).upper()


def is_valid(ref: str) -> bool:
    return _WHOLE.fullmatch(ref.strip()) is not None


def mentions(text: str) -> list[frozenset[str]]:
    """The order numbers mentioned in a message: one set of possible readings per mention.

    "ORD-261127-001 5 pm" can be read with or without the 5, so each run of space-separated units is a candidate.
    """
    found = []
    for match in _MENTION.finditer(text):
        run, candidates = "", set()
        for unit in match.group(1).split():
            run += unit.replace("-", "")
            if len(run) >= MIN_DIGITS:
                candidates.add("ORD" + run)
        if candidates:
            found.append(frozenset(candidates))
    return found
