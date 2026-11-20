"""A deterministic rule-based stand-in for a model. For load tests and demos only.

It is NOT a model and says nothing about model quality: it reads minutes out of the driver's text and
calls the same tools a model would, so the API, services and database do real work under load.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from setuhaul.model_gateway import ModelMessage, ModelPolicy, ModelResponse, ToolCall

_MIN = re.compile(r"(\d{1,3})\s*(?:minutes?|mins?)", re.IGNORECASE)


def _facts(system: str) -> dict[str, Any]:
    marker = "not by the driver)\n"
    return json.loads(system.split(marker, 1)[1]) if marker in system else {}


class StubGateway:
    def complete(self, messages: Sequence[ModelMessage], tools: Sequence[dict[str, Any]], policy: ModelPolicy) -> ModelResponse:
        facts = _facts(messages[0].content)
        last = messages[-1]
        if last.role == "tool":
            data = json.loads(last.content)
            if last.name == "search_slot_options" and data.get("status") == "OK":
                opts = "; ".join(f"{o['rank']}) {o['start']} dock {o['dock']}" for o in data["options"])
                return ModelResponse(text=f"Here are your options (none is held yet): {opts}")
            if data.get("status") == "NEEDS_INPUT":
                return ModelResponse(text=data.get("ask_the_driver", "Can you tell me more?"))
            return ModelResponse(text="Thanks, noted.")
        text = last.content
        low = text.lower()
        shipment = (facts.get("active_shipment_ids") or [None])[0]
        if "confirm" in low and facts.get("live_hold"):
            return ModelResponse(tool_calls=(ToolCall(id="s1", name="confirm_reschedule", arguments={
                "hold_appointment_id": facts["live_hold"]["appointment_id"], "driver_quote": "confirm"}),))
        if ("first" in low or "take" in low) and facts.get("latest_option_set_id"):
            quote = "first" if "first" in low else "take"
            return ModelResponse(tool_calls=(ToolCall(id="s1", name="request_hold", arguments={
                "option_set_id": facts["latest_option_set_id"], "rank": 1, "driver_quote": quote}),))
        m = _MIN.search(text)
        if m and shipment and ("late" in low or "delay" in low):
            return ModelResponse(tool_calls=(ToolCall(id="s1", name="search_slot_options", arguments={
                "shipment_id": shipment, "report": {"kind": "late_by", "minutes": int(m.group(1))}}),))
        return ModelResponse(text="How late are you, and which shipment is it?")
