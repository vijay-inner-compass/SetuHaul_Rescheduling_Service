"""The Dock Access Pass code: DAP- and 128 random bits in base32. Pure functions.

Only the hash is ever stored. Anything that looks like a code is blanked before a message is kept or shown to a model,
so the patterns also catch codes typed without DAP, with different spacing, or cut short.
"""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import base64
import hashlib
import re
import secrets

HIDDEN = "DAP-[hidden]"
_C = "[A-Z2-7]"
# The code in its groups of five (the last has six), with single spaces or hyphens, or run together.
_GROUPS = rf"(?:{_C}{{5}}[ -]?){{4}}{_C}{{6}}"
_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    rf"DAP[ \t\r\n-]*(?P<full>{_GROUPS})(?![A-Za-z0-9-])"
    # A mistyped or cut-off code after "DAP-".
    r"|DAP-(?P<near>[A-Z0-9-]{7,})"
    # Without DAP: run together, or grouped (grouped needs a digit, so five-letter words are left alone).
    rf"|(?P<bare>{_C}{{26}}|(?=[A-Z -]*[2-7]){_GROUPS})(?![A-Za-z0-9])"
    r")", re.IGNORECASE)


def new_code() -> str:
    body = base64.b32encode(secrets.token_bytes(16)).decode().rstrip("=")
    return "-".join(("DAP", body[:5], body[5:10], body[10:15], body[15:20], body[20:]))


def normalise(text: str) -> str:
    # Case, spaces and hyphens are formatting.
    return re.sub(r"[\s-]", "", text).upper()


def hash_code(code: str) -> str:
    # 128 random bits need no salt or stretching.
    return hashlib.sha256(normalise(code).encode()).hexdigest()


def find(text: str) -> list[str]:
    """Every code or near-miss in the text, normalised and starting with DAP."""
    return ["DAP" + normalise(m.group("full") or m.group("near") or m.group("bare")) for m in _PATTERN.finditer(text)]


def blank(text: str) -> str:
    return _PATTERN.sub(HIDDEN, text)
