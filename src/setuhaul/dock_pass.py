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
import unicodedata

HIDDEN = "DAP-[hidden]"
_C = "[A-Z2-7]"
# Whatever people put between the groups: spaces, line breaks and ordinary punctuation.
_SEP = r"""[\s\-_.,/\\|:;*`~'"+]"""
# Characters that only look like nothing, and the dashes that are not "-". NFKC deals with full-width forms.
_INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u2060\ufeff]")
_DASHES = re.compile("[\u2010-\u2015\u2212\ufe58\ufe63]")
# The code in its groups of five (the last has six), or run together.
_GROUPS = rf"(?:{_C}{{5}}{_SEP}*){{4}}{_C}{{6}}"
_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    # A whole code after DAP.
    rf"DAP{_SEP}*(?P<full>{_GROUPS})(?![A-Za-z0-9])"
    # A mistyped or cut-off code after "DAP-".
    rf"|DAP\s*-\s*(?P<near>[A-Z0-9]+(?:{_SEP}+[A-Z0-9]{{4,6}}(?![A-Za-z0-9]))*)(?<=[A-Z0-9]{{4}})"
    # A whole code without DAP.
    rf"|(?P<run>{_C}{{26}})(?![A-Za-z0-9])"
    rf"|(?P<grouped>(?=[A-Z\s\-]*[2-7])(?:{_C}{{5}}[\s\-]+){{4}}{_C}{{6}})(?![A-Za-z0-9])"
    # Anything else shaped like a damaged code: blanked, never counted. Three or more five-character groups with a digit,
    # or a very long run of letters and digits.
    rf"|(?=(?:[A-Z]|{_SEP})*[0-9])(?:[A-Z0-9]{{5}}{_SEP}+){{3,}}[A-Z0-9]{{3,8}}(?![A-Za-z0-9])"
    r"|[A-Z0-9]{20,}"
    r")", re.IGNORECASE)


def _clean(text: str) -> str:
    return _DASHES.sub("-", _INVISIBLE.sub("", unicodedata.normalize("NFKC", text)))


def new_code() -> str:
    body = base64.b32encode(secrets.token_bytes(16)).decode().rstrip("=")
    return "-".join(("DAP", body[:5], body[5:10], body[10:15], body[15:20], body[20:]))


def normalise(text: str) -> str:
    # Case, spacing and punctuation between groups are formatting.
    return re.sub(_SEP, "", _clean(text)).upper()


def hash_code(code: str) -> str:
    # 128 random bits need no salt or stretching.
    return hashlib.sha256(normalise(code).encode()).hexdigest()


def _codes(text: str, kinds: tuple[str, ...]) -> list[str]:
    out = []
    for m in _PATTERN.finditer(_clean(text)):
        value = next((m.group(k) for k in kinds if m.group(k)), None)
        if value:
            out.append("DAP" + normalise(value))
    return out


def find(text: str) -> list[str]:
    """Everything in the text that could be someone's code, normalised and starting with DAP."""
    return _codes(text, ("full", "near", "run", "grouped"))


def guesses(text: str) -> list[str]:
    """The finds that are clearly meant as a pass, so a wrong one counts as an attempt. Grouped words without DAP do not."""
    return _codes(text, ("full", "near", "run"))


def blank(text: str) -> str:
    """The text with anything shaped like a code removed; untouched when there is nothing to remove."""
    cleaned = _clean(text)
    return _PATTERN.sub(HIDDEN, cleaned) if _PATTERN.search(cleaned) else text
