"""Plain words and readable times for the screens. Stored codes never change; only what a person reads does."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from datetime import datetime

from setuhaul.db import IST

# Why a case was handed to a person.
REASON_TEXT = {
    "NO_SLOT": "No slot fits this load", "CONTRADICTORY_DATA": "Driver's report contradicts the gate record",
    "INCOMPLETE_FEASIBILITY_DATA": "Cannot check labour or customer data", "APPROVAL_REQUIRED": "Customer commitment needs approval",
    "SAFETY": "Safety concern", "REGULATED_LOAD": "Regulated load", "POLICY_CONFLICT": "Policy conflict",
    "DATABASE_UNAVAILABLE": "System unavailable during the conversation", "MODEL_UNAVAILABLE": "The assistant could not answer",
    "MODEL_LOOP_LIMIT": "The assistant hit its safety limit", "LANGUAGE_UNSUPPORTED": "Unsupported language",
    "CANCELLATION_CUTOFF": "Cancellation too close to the slot", "CUSTOMER_COMMITMENT": "Customer commitment at stake",
    "CUSTOMER_COMMITMENT_UNKNOWN": "Customer commitment unknown", "DRIVER_REQUESTED": "Driver asked for a person",
    "OTHER_UNCERTAINTY": "The assistant was unsure",
}
# Codes whose plain form is more than the code in sentence case.
_WORDS = {
    "SECURITY": "Security (gate)", "UNBOUND": "Not linked", "BOUND": "Linked", "SUPPLIED": "Supplied data",
    "PENDING_APPROVAL": "Waiting for approval", "PENDING_PARTNER": "Waiting for partner", "HANDOFF": "Handed to a person",
}


def word(code: object) -> str:
    # A code nobody has worded yet still reads as words.
    text = "" if code is None else str(code)
    return _WORDS.get(text, text.replace("_", " ").capitalize())


def reason(code: str) -> str:
    return REASON_TEXT.get(code, word(code))


def moment(stamp: str | None) -> str:
    # "04 Aug 2026, 09:35" in India time; nothing recorded reads "never", an unreadable value is shown as it is.
    if not stamp:
        return "never"
    try:
        return f"{datetime.fromisoformat(stamp).astimezone(IST):%d %b %Y, %H:%M}"
    except ValueError:
        return stamp
