"""Model + tool loop (S1, S2). The model proposes; tools/code decide. Round cap 6 (S2).
Model interface: invoke(system, messages, tool_specs) -> {"text": str, "tool_calls": [{"id","name","args"}]}"""
import json
import config

SYSTEM = """You help SetuHaul truck drivers who are running late.
- Ask the driver ONLY for facts the backend does not hold (delay, whether it includes repair time, earliest/latest time they can reach the dock). Call get_my_shipments first; never ask for vehicle, facility or appointment.
- find_options only SHOWS slots; nothing is reserved. Hold only after the driver says yes to one option; confirm only after they confirm.
- Say 'confirmed' only after confirm_booking returned ok. Never promise a slot yourself.
- If a tool says taken/expired/infeasible, explain why in plain words and offer fresh options.
- If no feasible slot exists, or the driver mentions safety, a penalty, or contradictory facts, call escalate_to_human and tell them a coordinator will contact them."""

def run_loop(model, specs, call, history, query, tool_log):
    msgs = [dict(role=m["role"], content=m["content"]) for m in history] + [dict(role="user", content=query)]
    rounds = 0
    resp = model.invoke(SYSTEM, msgs, specs())
    while resp["tool_calls"]:
        rounds += 1
        if rounds > config.MAX_TOOL_ROUNDS: raise RuntimeError("Tool-call limit reached")
        msgs.append(dict(role="assistant", content=resp["text"], tool_calls=resp["tool_calls"]))   # append the reply BEFORE its tool messages (S1)
        for tc in resp["tool_calls"]:
            out = call(tc["name"], tc["args"]); tool_log.append((tc["name"], out))
            msgs.append(dict(role="tool", tool_call_id=tc["id"], name=tc["name"], content=json.dumps(out)))
        resp = model.invoke(SYSTEM, msgs, specs())
    return resp["text"]

class ScriptedModel:
    """Deterministic stand-in for the LLM: steps are callables(msgs)->response. Free, repeatable tests."""
    def __init__(self, steps): self.steps = list(steps)
    def invoke(self, system, msgs, specs): return self.steps.pop(0)(msgs)

class AnthropicModel:
    """Real model adapter (not exercised by the tests; needs `pip install anthropic` and ANTHROPIC_API_KEY)."""
    def __init__(self, model="claude-sonnet-5-5"):
        import anthropic; self.cli, self.model = anthropic.Anthropic(), model
    def invoke(self, system, msgs, specs):
        out = []
        for m in msgs:
            if m["role"] == "assistant" and m.get("tool_calls"):
                blocks = ([dict(type="text", text=m["content"])] if m["content"] else []) + \
                         [dict(type="tool_use", id=t["id"], name=t["name"], input=t["args"]) for t in m["tool_calls"]]
                out.append(dict(role="assistant", content=blocks))
            elif m["role"] == "tool":
                blk = dict(type="tool_result", tool_use_id=m["tool_call_id"], content=m["content"])
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list): out[-1]["content"].append(blk)
                else: out.append(dict(role="user", content=[blk]))
            else: out.append(dict(role=m["role"], content=m["content"]))
        r = self.cli.messages.create(model=self.model, max_tokens=800, system=system, messages=out, tools=specs, temperature=0)
        return dict(text="".join(b.text for b in r.content if b.type == "text"),
                    tool_calls=[dict(id=b.id, name=b.name, args=b.input) for b in r.content if b.type == "tool_use"])
