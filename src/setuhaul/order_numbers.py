"""Order numbers as the warehouse enters them: ORD followed by at least six digits. Business information, not access."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import re

# A whole value as the warehouse enters it.
_WHOLE = re.compile(r"ORD[ -]*[0-9](?:[ -]*[0-9]){5,}", re.IGNORECASE)


def normalise(ref: str) -> str:
    # Case, spaces and hyphens are formatting, not identity.
    return re.sub(r"[\s-]", "", ref).upper()


def is_valid(ref: str) -> bool:
    return _WHOLE.fullmatch(ref.strip()) is not None
