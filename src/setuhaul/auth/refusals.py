"""Which field a malformed request got wrong, in words, without repeating anything that was sent."""

# Lazy annotations.
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from setuhaul.identity import AuthError

Problem = tuple[str, str]
# Enough to act on, and a hostile body cannot make the answer large.
_MOST = 10
# The wording for each kind of validation failure. {name} is a limit from our own rules, never from the request.
_WORDS = {
    "missing": "is missing", "string_too_long": "must be at most {max_length} characters",
    "greater_than": "must be more than {gt}", "greater_than_equal": "must be {ge} or more",
    "less_than": "must be less than {lt}", "less_than_equal": "must be {le} or less",
    "too_short": "needs at least {min_length} chosen", "too_long": "allows at most {max_length}",
    "literal_error": "is not one of the allowed choices", "string_pattern_mismatch": "is not in the expected format",
}
# The same, for families of kinds that start alike.
_FAMILIES = (("datetime_", "is not a valid date and time"), ("date_", "is not a valid date"), ("time_", "is not a valid time"),
             ("int_", "must be a whole number"), ("float_", "must be a number"), ("decimal_", "must be a number"),
             ("bool_", "must be Yes or No"))


class InvalidRequest(AuthError):
    """INVALID_REQUEST from a hand-written check, naming each field at fault."""

    def __init__(self, *problems: Problem) -> None:
        super().__init__("INVALID_REQUEST")
        self.problems = problems


def _words(kind: str, limits: Mapping[str, Any]) -> str:
    if kind == "string_too_short":
        least = limits.get("min_length", 1)
        return "is missing" if least <= 1 else f"must be at least {least} characters"
    if kind in _WORDS:
        return _WORDS[kind].format_map(limits)
    return next((words for start, words in _FAMILIES if kind.startswith(start)), "is not valid")


def problems_from(errors: Iterable[Mapping[str, Any]], skip: int = 0) -> list[Problem]:
    """(field, problem) for each pydantic error. `skip` drops the leading "body" or "query" of a request's location."""
    found: list[Problem] = []
    for error in errors:
        where: Sequence[Any] = error.get("loc", ())[skip:]
        field = str(where[0]) if where and isinstance(where[0], str) else ""
        kind = str(error.get("type", ""))
        if kind == "extra_forbidden":
            # The name came from the caller, so it is not repeated.
            problem = ("", "The request has a field the service does not accept")
        elif not field:
            problem = ("", "The request could not be read")
        else:
            problem = (field, _words(kind, error.get("ctx", {})))
        if problem not in found:
            found.append(problem)
    return found[:_MOST]


def details(problems: Sequence[Problem]) -> dict[str, Any]:
    """What a refusal adds to its answer: a sentence, and the fields so a screen can use its own labels."""
    if not problems:
        return {}
    sentences = [f"{field.replace('_', ' ').capitalize()}: {text}." if field else f"{text}." for field, text in problems]
    return {"message": " ".join(sentences) + " Nothing was changed.",
            "problems": [{"field": field, "problem": text} for field, text in problems if field]}


def details_of(exc: AuthError) -> dict[str, Any]:
    return details(exc.problems) if isinstance(exc, InvalidRequest) else {}
