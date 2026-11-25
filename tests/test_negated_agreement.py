"""A refusal that negates an agreeing word ("nahi chalega") must be read as a refusal.

Found in a real-model run on 2026-11-26: the Hindi and Hinglish refusals were ignored and the refused slot was later booked.
"""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import pytest

from setuhaul.tools import sounds_like_agreement
from tests.test_auto_hold import LATE, Chat, chat  # noqa: F401 - `chat` is the fixture


@pytest.mark.parametrize("text", [
    "nahi, yeh slot nahi chalega, agla slot dijiye", "नहीं, यह स्लॉट नहीं चलेगा, अगला स्लॉट दीजिए", "nahi chalegi",
    "chalega nahi, dusra slot", "yeh slot theek nahi hai, agla dijiye", "यह ठीक नहीं है, अगला दीजिए", "accha nahi hai yeh",
    "that is not good, next slot", "not fine, give me the next one", "this isn't ok", "this isn’t okay", "no good, next please",
    "that is not so good", "don't confirm that", "confirm mat karo", "कन्फर्म मत कीजिए",
])
def test_a_negated_agreement_word_is_not_agreement(text: str) -> None:
    assert not sounds_like_agreement(text)


@pytest.mark.parametrize("text", [
    "nahi theek hai", "nahi, chalega", "why not, sure", "not a problem, yes", "theek hai, nahi to late ho jaunga",
    "not the first one but this is fine", "haan chalega",
])
def test_unclear_or_agreeing_messages_still_cannot_decline(text: str) -> None:
    assert sounds_like_agreement(text)


def test_a_hinglish_refusal_moves_to_the_next_slot(chat: Chat) -> None:  # noqa: F811
    chat.say(LATE)
    first = [r["slot_id"] for r in chat.held()]
    chat.say("nahi, yeh slot nahi chalega, agla slot dijiye")
    after = [r["slot_id"] for r in chat.held()]
    assert len(after) == 1 and after != first
