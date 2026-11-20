import json, pytest
import app, capacity, agent, database
from agent import ScriptedModel
from observability import Tracer
from seed import NOW

def last_tool(msgs, name):
    for m in reversed(msgs):
        if m["role"] == "tool" and m["name"] == name: return json.loads(m["content"])
def tc(i, name, **args): return dict(id=f"c{i}", name=name, args=args)
def say(text): return lambda m: dict(text=text, tool_calls=[])
def call(*tcs): return lambda m: dict(text="", tool_calls=list(tcs))

def test_full_scenario_delay_options_hold_confirm(c):
    tr = Tracer()
    t1 = app.chat(c, "tok-001", "th1", "About two hours late, I think", ScriptedModel([
        call(tc(1, "get_my_shipments")),
        call(tc(2, "report_delay", shipment_id="SHP1001", delay_minutes=120)),
        call(tc(3, "find_options", shipment_id="SHP1001", limit=2)),
        lambda m: dict(text="Options (not reserved): " + ", ".join(o["slot_id"] for o in last_tool(m, "find_options")["options"]), tool_calls=[])]), NOW, tr)
    assert "not reserved" in t1["reply"] and c.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 0
    assert capacity.find_options(c, "SHP1001", NOW)[0]["start"] >= "2026-08-04T11:15"
    t2 = app.chat(c, "tok-001", "th1", "The second one works", ScriptedModel([
        call(tc(1, "hold_option", shipment_id="SHP1001", slot_id="D2-12", idempotency_key="th1-hold-0001")),
        lambda m: dict(text=f"Kept for you until {last_tool(m, 'hold_option')['held_until']}.", tool_calls=[])]), NOW, tr)
    assert "Kept for you" in t2["reply"] and t2["guard"] is None
    t3 = app.chat(c, "tok-001", "th1", "Yes confirm", ScriptedModel([
        call(tc(1, "get_my_shipments")),
        lambda m: call(tc(2, "confirm_booking", hold_id=last_tool(m, "get_my_shipments")["shipments"][0]["appointment_id"]))(m),
        say("Confirmed: dock D2 at 12:00.")]), NOW, tr)
    assert "Confirmed" in t3["reply"] and t3["guard"] is None
    assert c.execute("SELECT status FROM appointments").fetchone()[0] == "confirmed"
    assert tr.tool_calls_per_trace(t3["request_id"]) == 2
    assert len(app.memory.load_history(c, "DRV001", "th1")) == 6

def test_false_confirmation_is_blocked(c):
    r = app.chat(c, "tok-001", "th2", "ok book it", ScriptedModel([say("You're all set, booked for 12:00!")]), NOW, Tracer())
    assert r["guard"] == "false_confirmation_blocked" and "booked" not in r["reply"].lower().replace("not confirmed a booking", "")
    assert c.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 0

def test_identity_foreign_shipment_gets_nothing(c):
    r = app.chat(c, "tok-001", "th3", "options for 1002", ScriptedModel([
        call(tc(1, "find_options", shipment_id="SHP1002")), say("done")]), NOW, Tracer())
    out = r["tool_log"][0][1]
    assert out["ok"] is False and out["code"] == "not_found"
    assert "driver_id" not in json.dumps(agent.__dict__.get("SYSTEM", ""))

def test_invalid_arguments_reach_model_as_data(c):
    r = app.chat(c, "tok-001", "th4", "late", ScriptedModel([
        call(tc(1, "report_delay", shipment_id="SHP1001", delay_minutes=-5)), say("sorry")]), NOW, Tracer())
    assert r["tool_log"][0][1]["code"] == "invalid_arguments"

def test_no_feasible_slot_escalates_with_packet(c):
    c.execute("UPDATE shipments SET vehicle_type='reefer', current_eta='2026-08-04T17:00' WHERE shipment_id='SHP1001'")
    r = app.chat(c, "tok-001", "th5", "stuck at 5pm", ScriptedModel([
        call(tc(1, "find_options", shipment_id="SHP1001")),
        lambda m: call(tc(2, "escalate_to_human", shipment_id="SHP1001", reason="no feasible reefer slot after 17:15"))(m),
        say("A coordinator will contact you. I can't promise a slot.")]), NOW, Tracer())
    assert last_tool([dict(role="tool", name=n, content=json.dumps(o)) for n, o in r["tool_log"]], "find_options")["options"] == []
    assert r["escalated"]
    pk = json.loads(c.execute("SELECT packet_json FROM escalations").fetchone()[0])
    assert pk["shipment"]["shipment_id"] == "SHP1001" and pk["reason"] and pk["recent_messages"] is not None

def test_emergency_escalates_without_calling_model(c):
    class Boom:
        def invoke(self, *a): raise AssertionError("model must not be called")
    r = app.chat(c, "tok-001", "th6", "there was an accident, driver hurt", Boom(), NOW, Tracer())
    assert r["escalated"] and c.execute("SELECT COUNT(*) FROM escalations").fetchone()[0] == 1

def test_bad_token_rejected(c):
    with pytest.raises(PermissionError): app.chat(c, "nope", "t", "hi", ScriptedModel([]), NOW, Tracer())

def test_round_cap(c):
    loop = lambda m: dict(text="", tool_calls=[tc(1, "get_my_shipments")])
    with pytest.raises(RuntimeError): app.chat(c, "tok-001", "th7", "x", ScriptedModel([loop] * 10), NOW, Tracer())
