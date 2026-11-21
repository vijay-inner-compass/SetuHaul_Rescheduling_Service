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


class StubGateway:
    def complete(self, messages: Sequence[ModelMessage], tools: Sequence[dict[str, Any]], policy: ModelPolicy) -> ModelResponse:
        # What the system told the model: live hold, latest option set, active shipments.
        facts = _facts(messages[0].content)
        # The most recent message decides what to do next.
        last = messages[-1]
        # If the last message is a tool result, we are mid-turn and must turn that result into words.
        if last.role == "tool":
            data = json.loads(last.content)
            # A successful search: list the options (the system re-words this as a numbered menu anyway).
            if last.name == "search_slot_options" and data.get("status") == "OK":
                opts = "; ".join(f"{o['rank']}) {o['start']} dock {o['dock']}" for o in data["options"])
                # When GPS disagreed with the driver, add one neutral sentence (the key is absent otherwise).
                note = (f" Note: your truck's GPS suggests your arrival differs from your report by about "
                        f"{data['gps_discrepancy_minutes']} minutes.") if "gps_discrepancy_minutes" in data else ""
                return ModelResponse(text=f"Here are your options (none is held yet): {opts}{note}")
            # The tool needs more information: pass its question straight through.
            if data.get("status") == "NEEDS_INPUT":
                return ModelResponse(text=data.get("ask_the_driver", "Can you tell me more?"))
            # Anything else: a neutral acknowledgement.
            return ModelResponse(text="Thanks, noted.")
        # Otherwise the last message is the driver's own text.
        text = last.content
        low = text.lower()
        # The first active shipment is assumed to be the one meant.
        shipment = (facts.get("active_shipment_ids") or [None])[0]
        # "confirm" while a live hold exists: call the confirm tool with the driver's word as the quote.
        if "confirm" in low and facts.get("live_hold"):
            return ModelResponse(tool_calls=(ToolCall(id="s1", name="confirm_reschedule", arguments={
                "hold_appointment_id": facts["live_hold"]["appointment_id"], "driver_quote": "confirm"}),))
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
        # "NN minutes late/delay": search for slots using that many minutes of delay.
        m = _MIN.search(text)
        if m and shipment and ("late" in low or "delay" in low):
            return ModelResponse(tool_calls=(ToolCall(id="s1", name="search_slot_options", arguments={
                "shipment_id": shipment, "report": {"kind": "late_by", "minutes": int(m.group(1))}}),))
        # Nothing recognised: ask the generic question.
        return ModelResponse(text="How late are you, and which shipment is it?")
