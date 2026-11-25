"""A deterministic rule-based stand-in for a model. For load tests and demos only.

It is NOT a model and says nothing about model quality: it reads minutes out of the driver's text and
calls the same tools a model would, so the API, services and database do real work under load.
"""

# Lazy annotations.
from __future__ import annotations

# json decodes the facts block and tool results.
import json

# re finds "NN minutes" in the driver's text.
import re

# Sequence for read-only list parameters.
from collections.abc import Sequence

# Any for loosely typed dictionaries.
from typing import Any

# The same message and response types a real provider adapter uses, so the stub is a drop-in gateway.
from setuhaul.model_gateway import ModelMessage, ModelPolicy, ModelResponse, ToolCall

# Matches "60 minutes", "5 min", "30 mins": one to three digits then a minutes word.
# A bare menu choice: "3", "option 3", "number 3" or "no. 3" (digits 1 to 9; the tool layer rejects ranks outside 1 to 3).
_PICK = re.compile(r"(?:(?:option|number|no\.?)\s*)?([1-9])")
_MIN = re.compile(r"(\d{1,3})\s*(?:minutes?|mins?)", re.IGNORECASE)


def _facts(system: str) -> dict[str, Any]:
    # The system prompt ends with a facts block (JSON) after this marker line; the stub reads it back out.
    marker = "not by the driver)\n"
    return json.loads(system.split(marker, 1)[1]) if marker in system else {}


# Hours written as digits ("2 hours", "1.5 hrs", "2 ghante") or as words ("an hour", "one hour", "two hours", "half an hour").
_HOURS = re.compile(r"(\d{1,2}(?:\.\d)?)\s*(?:hours?|hrs?|ghante|ghanta|h)\b")
_WORD_HOURS = {"an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
_HOUR_WORDS = re.compile(r"\b(an|one|two|three|four|five|six)\s+(?:hours?|hrs?)\b")


def _delay_minutes(low: str) -> int:
    # Total delay in minutes found in lower-case driver text, or 0 when none is stated.
    total = 0.0
    # Hours written as digits.
    for h in _HOURS.finditer(low):
        total += float(h.group(1)) * 60
    # "half an hour" or "half hour" (checked first, because "an hour" inside it must not also count as a whole hour).
    half = re.search(r"\bhalf (?:an )?hour\b", low)
    # Hours written as words ("two hours"); skipped when a digit form matched or the phrase is "half an hour".
    if not _HOURS.search(low) and not half:
        w = _HOUR_WORDS.search(low)
        if w:
            total += _WORD_HOURS[w.group(1)] * 60
    # Add the half hour.
    if half:
        total += 30
    # Plus any explicit minutes ("1 hour 30 minutes").
    m = _MIN.search(low)
    if m:
        total += int(m.group(1))
    return int(total)


class StubGateway:
    def complete(self, messages: Sequence[ModelMessage], tools: Sequence[dict[str, Any]], policy: ModelPolicy) -> ModelResponse:
        # What the system told the model: live hold, latest option set, active shipments.
        facts = _facts(messages[0].content)
        # The most recent message decides what to do next.
        last = messages[-1]
        # If the last message is a tool result, we are mid-turn and must turn that result into words.
        if last.role == "tool":
            data = json.loads(last.content)
            # A successful search: the system has held a slot and words it itself, so the model only acknowledges.
            if last.name == "search_slot_options" and data.get("status") == "OK":
                return ModelResponse(text="One slot is held for you.")
            # The tool needs more information: pass its question straight through.
            if data.get("status") == "NEEDS_INPUT":
                return ModelResponse(text=data.get("ask_the_driver", "Can you tell me more?"))
            # A cancellation that could not be done: say why in plain words (a cancellation that worked is worded by the system).
            if last.name == "cancel_appointment" and data.get("status") == "NOT_FOUND":
                return ModelResponse(text="I could not find an appointment to cancel.")
            if last.name == "cancel_appointment" and data.get("status") == "NOT_CANCELLABLE":
                return ModelResponse(text="Unloading has already started, so this appointment cannot be cancelled here.")
            # "Never mind" with no hold to release.
            if last.name == "release_pending_change" and data.get("status") == "NOTHING_PENDING":
                return ModelResponse(text="There is no pending change to release. Your original appointment stands.")
            # Anything else: a neutral acknowledgement.
            return ModelResponse(text="Thanks, noted.")
        # Otherwise the last message is the driver's own text.
        text = last.content
        low = text.lower()
        # The first active shipment is assumed to be the one meant.
        shipment = (facts.get("active_shipment_ids") or [None])[0]
        # The shipment a cancel or release is about: the thread's selected one, else the first active one.
        target = facts.get("selected_shipment_id") or shipment
        # "change facility to <city>": ask the destination.
        if "facility" in low and "change" in low and shipment:
            city = "FAC-GGN-01" if "gurugram" in low else "FAC-PARTNER-01" if "partner" in low else "FAC-JAI-01"
            return ModelResponse(tool_calls=(ToolCall(id="s1", name="request_facility_change", arguments={
                "shipment_id": target or shipment, "facility_id": city, "driver_quote": "change"}),))
        # "no" (and nothing agreeing) while a slot is held: ask for the next one.
        refused = re.match(r"(no|nahi)\b", low.strip())
        if refused and facts.get("live_hold") and "yes" not in low and "confirm" not in low:
            return ModelResponse(tool_calls=(ToolCall(id="s1", name="decline_held_slot", arguments={"driver_quote": refused.group(1)}),))
        # "never mind" / "keep my original slot" while a hold exists: release the hold; the original appointment is untouched.
        keep = next((w for w in ("never mind", "keep my original", "original slot", "original") if w in low), None)
        if keep and target and facts.get("live_hold"):
            return ModelResponse(tool_calls=(ToolCall(id="s1", name="release_pending_change", arguments={
                "shipment_id": target, "driver_quote": keep}),))
        # "cancel": cancel the confirmed appointment (the rules may instead hand it to a person).
        if "cancel" in low and target:
            return ModelResponse(tool_calls=(ToolCall(id="s1", name="cancel_appointment", arguments={
                "shipment_id": target, "driver_quote": "cancel"}),))
        # "confirm" while a live hold exists: call the confirm tool with the driver's word as the quote.
        agreed = "confirm" if "confirm" in low else "yes" if re.match(r"yes\b", low.strip()) else None
        if agreed and facts.get("live_hold"):
            return ModelResponse(tool_calls=(ToolCall(id="s1", name="confirm_reschedule", arguments={
                "hold_appointment_id": facts["live_hold"]["appointment_id"], "driver_quote": agreed}),))
        # A menu number ("3", "option 2", "number 1") after options were shown: hold that line, quoting the driver's own text.
        pick = _PICK.fullmatch(low.strip())
        if pick and facts.get("latest_option_set_id"):
            return ModelResponse(tool_calls=(ToolCall(id="s1", name="request_hold", arguments={
                "option_set_id": facts["latest_option_set_id"], "rank": int(pick.group(1)), "driver_quote": low.strip()}),))
        # "first" or "take" after options were shown: hold option 1.
        if ("first" in low or "take" in low) and facts.get("latest_option_set_id"):
            quote = "first" if "first" in low else "take"
            return ModelResponse(tool_calls=(ToolCall(id="s1", name="request_hold", arguments={
                "option_set_id": facts["latest_option_set_id"], "rank": 1, "driver_quote": quote}),))
        # "NN minutes", "2 hours", "an hour", "half an hour" or "1 hour 30 minutes" with late/delay: search with that delay.
        minutes = _delay_minutes(low)
        if minutes and shipment and ("late" in low or "delay" in low):
            return ModelResponse(tool_calls=(ToolCall(id="s1", name="search_slot_options", arguments={
                "shipment_id": shipment, "report": {"kind": "late_by", "minutes": minutes}}),))
        # Nothing recognised: ask the generic question.
        return ModelResponse(text="How late are you, and which shipment is it?")
