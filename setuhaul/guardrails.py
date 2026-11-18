"""Live controls, cheapest first (S11): code -> config -> regex -> LLM last. Place: input / action / output."""
import re
EMERGENCY = re.compile(r"\b(accident|injur\w*|hospital|fire|crash\w*|hazmat|chemical leak|police|stolen|hijack\w*|medical)\b", re.I)
CLAIMS_CONFIRMED = re.compile(r"(?<!not )(?<!n't )\b(confirmed|booked|you're all set)\b", re.I)
CLAIMS_HELD = re.compile(r"(?<!not )(?<!n't )\b(reserved|kept for you|on hold for you)\b", re.I)

def pre_input(msg):
    """Input placement: deterministic safety triggers hand to a human WITHOUT calling the model."""
    m = EMERGENCY.search(msg or "")
    return f"safety_or_emergency:{m.group(0).lower()}" if m else None

def post_output(reply, tool_log):
    """Output placement, verified-state check (S11 type 8): no 'confirmed' unless confirm_booking returned ok THIS turn."""
    ok = lambda n: any(t == n and r.get("ok") for t, r in tool_log)
    if CLAIMS_CONFIRMED.search(reply) and not ok("confirm_booking"):
        return "I have not confirmed a booking yet. Tell me which option you want and I will check it is still free.", "false_confirmation_blocked"
    if CLAIMS_HELD.search(reply) and not (ok("hold_option") or ok("confirm_booking")):
        return "Nothing is held for you yet. Tell me which option you want and I will try to hold it.", "false_hold_blocked"
    return reply, None
