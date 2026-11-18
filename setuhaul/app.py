"""Driver file (S5): receives the request, runs five steps in order, never cooks."""
import sessions, memory, guardrails, escalation, agent
from tools import make_tools

def chat(c, token, thread_id, query, model, now, tracer):
    rid = tracer.new_request()
    with tracer.span("chat", thread=thread_id):
        driver = sessions.require_driver(c, token)                                   # 1 who
        did = driver["driver_id"]
        trig = guardrails.pre_input(query)                                           # 2 input guard (no LLM)
        if trig:
            sh = c.execute("SELECT shipment_id FROM shipments WHERE driver_id=? LIMIT 1", (did,)).fetchone()
            memory.save_message(c, did, thread_id, "user", query)
            escalation.escalate(c, did, thread_id, sh["shipment_id"] if sh else None, trig, now)
            reply = "I'm passing this to a coordinator now. They will contact you directly. If anyone is in danger, call emergency services."
            memory.save_message(c, did, thread_id, "assistant", reply)
            return dict(reply=reply, escalated=True, request_id=rid, tool_log=[], guard=trig)
        history = memory.load_history(c, did, thread_id)                             # 3 memory
        specs, call = make_tools(c, driver, thread_id, now, tracer)                  # tools built per request from token
        log = []
        with tracer.span("agent.loop"):
            text = agent.run_loop(model, specs, call, history, query, log)           # 4 model + tools
        reply, blocked = guardrails.post_output(text, log)                           # 5 output guard
        memory.save_message(c, did, thread_id, "user", query)
        memory.save_message(c, did, thread_id, "assistant", reply)
        esc = any(n == "escalate_to_human" and r.get("ok") for n, r in log)
        return dict(reply=reply, escalated=esc, request_id=rid, tool_log=log, guard=blocked)
