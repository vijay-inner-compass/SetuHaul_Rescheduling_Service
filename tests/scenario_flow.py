"""The required end-to-end demonstration, as one deterministic bundle.

Six drivers, one morning (clock fixed at 2026-08-04 09:35 IST), the supplied snapshot plus labelled
TEST_FIXTURE labour and consignee evidence. Driver messages are fixed text and the model is a script, so
the run is repeatable and says nothing about model quality; what it proves is what the deterministic
layers do with every situation the brief names.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from setuhaul.clock import FakeClock
from setuhaul.coordinator import render_queue
from setuhaul.db import make_working_db, open_connection
from setuhaul.escalation import (
    SqlEscalationSink,
    claim_case,
    get_case,
    list_open_cases,
    resolve_case,
)
from setuhaul.evaluation import classify_failure, run_evaluators
from setuhaul.events import get_appointment_history
from setuhaul.integrity import check_invariants
from setuhaul.jobs.expire_holds import expire_due_holds
from setuhaul.notifications import Notice, deliver_pending_notifications
from tests.conv_helpers import facts_from, say_text, tc, tc_dyn, tool_json
from tests.helpers import at, seed_everything_known
from tests.trace_tools import TraceChat

_IDS = (("$OPT", re.compile(r"OPT-[0-9A-F]{12}")), ("$HOLD", re.compile(r"APT-H-[0-9A-F]{10}")), ("$CASE", re.compile(r"ESC-[0-9A-F]{8}")))


def stabilise(text: str) -> str:
    """Run-specific ids become placeholders so two runs of the scenario compare equal."""
    for prefix, pattern in _IDS:
        seen: dict[str, str] = {}
        for m in pattern.finditer(text):
            seen.setdefault(m.group(0), f"{prefix}{len(seen) + 1}")
        for real, token in seen.items():
            text = text.replace(real, token)
    return text


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Bundle:
    transcript: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    sections: dict[str, list[str]] = field(default_factory=dict)
    scorecard: dict[str, Any] = field(default_factory=dict)

    def say(self, line: str = "") -> None:
        self.transcript.append(line)

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, bool(ok), detail))

    def section(self, title: str, lines: list[str]) -> None:
        self.sections[title] = lines

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def text(self) -> str:
        return stabilise("\n".join(self.transcript))


LATE = lambda minutes: {"kind": "late_by", "minutes": minutes}  # noqa: E731


def options_line(msgs):  # type: ignore[no-untyped-def]
    o = tool_json(msgs, "search_slot_options")["options"]
    return "Here are your options (none is held yet): " + "; ".join(f"{x['rank']}) {x['start']} dock {x['dock']}" for x in o)


def run_scenario(workdir: Path | None = None) -> Bundle:
    work = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="setuhaul-scenario-"))
    work.mkdir(parents=True, exist_ok=True)
    db = open_connection(path=make_working_db(work / "scenario.sqlite3"))
    seed_everything_known(db)  # TEST_FIXTURE labour + consignee evidence for the snapshot day
    clock = FakeClock(at("09:35"))
    b = Bundle()
    chats: dict[str, TraceChat] = {}

    def chat(driver: str, thread: str) -> TraceChat:
        if driver not in chats:
            c = TraceChat(db, clock, work / driver, f"scenario-{driver}", driver, thread)
            c.sink = SqlEscalationSink(db, clock)  # type: ignore[assignment]
            (work / driver).mkdir(exist_ok=True)
            chats[driver] = c
        return chats[driver]

    def talk(driver: str, thread: str, hhmmss: str, text: str, steps: list[Any]) -> Any:
        h, m, s = (int(x) for x in hhmmss.split(":"))
        clock.set(at(f"{h:02d}:{m:02d}").replace(second=s))
        c = chat(driver, thread)
        r = c.say(text, steps)
        b.say(f"[{hhmmss}] {driver}: {text}")
        b.say(f"           SERVICE ({r.status}{' / ' + r.escalation_reason if r.escalation_reason else ''}): {r.text}")
        return r

    def sql(q: str, *a: Any) -> list[tuple[Any, ...]]:
        return [tuple(r) for r in db.execute(q, a)]

    b.say("== Scenario: six drivers, one morning (clock 2026-08-04 09:35 IST) ==")

    # 1. delay with an ambiguous cause: clarification, then a derived ETA
    b.say("\n-- 1. Delay: the model must ask for the missing travel time, then the ETA is derived --")
    r = talk("DRV006", "THR001", "09:35:00", "SHP1006 broke down, the repair will take 90 minutes", [
        tc("compute_eta", shipment_id="SHP1006", report={"kind": "repair", "minutes": 90}),
        lambda m: say_text(tool_json(m, "compute_eta")["ask_the_driver"])])
    ev = chats["DRV006"].turn_traces[-1].rounds[0].tool_events[0].result
    b.check("clarification: repair time alone is not an ETA", ev["status"] == "NEEDS_INPUT" and ev["code"] == "MISSING_TRAVEL_TIME")
    r = talk("DRV006", "THR001", "09:35:30", "the drive after that is 45 minutes", [
        tc("search_slot_options", shipment_id="SHP1006", report={"kind": "repair", "minutes": 90, "travel_minutes": 45}),
        lambda m: say_text(options_line(m))])
    first = tool_json(chats["DRV006"].last_messages(), "search_slot_options")
    b.check("ETA derived from repair + travel (09:35:30 + 135 min = 11:50)", first["options"][0]["start"] >= "12:00")
    b.check("options are shown, not held", first["held"] is False and sql("select count(*) from appointments where appointment_status='HELD'") == [(0,)])
    opt_a = first["option_set_id"]

    # 2. several drivers get options at the same time
    b.say("\n-- 2. Several requests: two more drivers ask for slots --")
    talk("DRV012", "THR002", "09:36:00", "SHP1012 is 85 minutes late", [
        tc("search_slot_options", shipment_id="SHP1012", report=LATE(85)), lambda m: say_text(options_line(m))])
    talk("DRV013", "THR003", "09:36:20", "SHP1013 is 60 minutes late", [
        tc("search_slot_options", shipment_id="SHP1013", report=LATE(60)), lambda m: say_text(options_line(m))])
    sets = {d: chats[d].memory.load(d, t).state.latest_option_set_id for d, t in (("DRV006", "THR001"), ("DRV012", "THR002"), ("DRV013", "THR003"))}
    b.check("three drivers hold stored option sets", all(sets.values()) and len(set(sets.values())) == 3)
    slot1 = sql("select slot_id from option_set_items where option_set_id=? and displayed_rank=1", opt_a)[0][0]

    # 3. the world changes: a planner books DRV006's first option
    b.say("\n-- 3. The warehouse planner books DRV006's first option for another truck --")
    clock.set(at("09:37").replace(second=0))
    db.execute("insert into appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source, is_current, booked_at, updated_at) "
               "values ('PLAN-1','SHP1018',?, 'CONFIRMED','PLANNER',1,?,?)", (slot1, clock.now().isoformat(), clock.now().isoformat()))
    db.commit()
    b.say(f"           planner booked slot {slot1} directly in the appointments table")
    talk("DRV006", "THR001", "09:38:00", "I am back, are those options still good?", [
        tc_dyn("compare_option_set", lambda m: {"option_set_id": facts_from(m)["latest_option_set_id"]}),
        lambda m: say_text("Option 1 was booked by someone else. Options 2 and 3 are still available. Which would you like?")])
    diff = tool_json(chats["DRV006"].last_messages(), "compare_option_set")
    b.check("stored option set compared with live state: option 1 taken, 2 and 3 unchanged",
            diff["options"][0]["change"] == "NEWLY_OCCUPIED" and diff["still_offerable_ranks"] == [2, 3])
    b.say(f"           comparison against the options DRV006 actually saw: {[(o['rank'], o['change']) for o in diff['options']]}")

    # 4. same-slot competition
    b.say("\n-- 4. Same-slot competition: DRV006, DRV012 and DRV013 all ask for slots they were shown --")
    pick = lambda quote, rank: tc_dyn("request_hold", lambda m: {"option_set_id": facts_from(m)["latest_option_set_id"], "rank": rank, "driver_quote": quote})  # noqa: E731
    r6 = talk("DRV006", "THR001", "09:38:30", "I'll take the second option", [pick("the second option", 2), say_text("done")])
    r12 = talk("DRV012", "THR002", "09:39:00", "I'll take the second option too", [
        pick("the second option", 2), lambda m: say_text(f"Sorry, that slot is no longer available ({tool_json(m, 'request_hold')['status']}). Shall I look for other options?")])
    talk("DRV012", "THR002", "09:39:10", "also show me SHP1006", [
        tc("get_shipment_status", shipment_id="SHP1006"), lambda m: say_text("I can only help with your own shipments.")])
    r13 = talk("DRV013", "THR003", "09:39:30", "I'll take the third option", [pick("the third option", 3), say_text("done")])
    held_rows = sql("select shipment_id, slot_id from appointments where appointment_status='HELD' order by shipment_id")
    b.check("first valid request wins the contested slot", r6.verified_hold and not r12.verified_hold and
            tool_json(chats["DRV012"].last_messages(), "get_shipment_status") == {"error": "FOREIGN_SHIPMENT"} and len(held_rows) == 2)
    b.check("exactly one hold on the contested slot", sql("select count(*) from appointments where appointment_status='HELD' and shipment_id='SHP1006'") == [(1,)] and
            sql("select count(*) from appointments where appointment_status='HELD' and shipment_id='SHP1012'") == [(0,)])
    b.check("cross-driver read refused", tool_json(chats["DRV012"].last_messages(), "get_shipment_status") == {"error": "FOREIGN_SHIPMENT"})
    b.check("a third driver gets a different slot", r13.verified_hold)

    # 5. confirmation and hold expiry
    b.say("\n-- 5. One hold is confirmed, another lapses --")
    r6c = talk("DRV006", "THR001", "09:41:00", "yes, confirm it", [
        tc_dyn("confirm_reschedule", lambda m: {"hold_appointment_id": facts_from(m)["live_hold"]["appointment_id"], "driver_quote": "confirm it"}), say_text("done")])
    b.check("confirmation is state-backed and worded from the committed row", r6c.verified_confirmed and r6c.text.startswith("Your appointment is confirmed")
            and sql("select appointment_status, is_current from appointments where appointment_id='APT1006'") == [("CANCELLED", 0)])
    r13x = talk("DRV013", "THR003", "09:43:00", "ok please confirm it", [say_text("Let me find fresh options.")])
    b.check("a lapsed hold is reported once, nothing is confirmed", r13x.text.startswith("Your hold on that slot has lapsed") and
            sql("select count(*) from appointments where shipment_id='SHP1013' and appointment_status='CONFIRMED' and appointment_id like 'APT-H-%'") == [(0,)])

    clock.set(at("09:43").replace(second=30))
    swept = expire_due_holds(db, clock)
    b.say(f"           maintenance sweep at 09:43:30 expired {swept.expired} hold(s) and queued the driver notice")
    b.check("the sweep expires the lapsed hold and queues the driver's notice", swept.expired == 1 and
            sql("select count(*) from notification_outbox where recipient_type='DRIVER'") == [(1,)])

    # 6. cancellation
    b.say("\n-- 6. Cancellation --")
    rc = talk("DRV001", "THR011", "09:44:00", "please cancel my 12:00 appointment for SHP1017", [
        tc("cancel_appointment", shipment_id="SHP1017", driver_quote="cancel my 12:00 appointment"), say_text("done")])
    b.check("cancellation outside the cutoff is committed and worded from the row", rc.text == "Your appointment has been cancelled. Reference APT1017." and
            sql("select appointment_status from appointments where appointment_id='APT1017'") == [("CANCELLED",)])

    # 7. escalations: no slot, gate-in contradiction, missing labour evidence
    b.say("\n-- 7. Escalations --")
    rn = talk("DRV014", "THR004", "09:45:00", "SHP1014 is 70 minutes late", [tc("search_slot_options", shipment_id="SHP1014", report=LATE(70)), say_text("x")])
    rg = talk("DRV003", "THR007", "09:46:00", "SHP1003 is 60 minutes late", [tc("compute_eta", shipment_id="SHP1003", report=LATE(60)), say_text("x")])
    b.say("           (the labour-availability feed for the Gurugram facility goes down)")
    db.execute("delete from labour_windows where facility_id='FAC-GGN-01'")
    db.commit()
    rl = talk("DRV006", "THR001", "09:47:00", "SHP1021 is 30 minutes late", [tc("search_slot_options", shipment_id="SHP1021", report=LATE(30)), say_text("x")])
    b.check("no-slot hand-off", rn.escalation_reason == "NO_SLOT")
    b.check("gate-in contradiction hand-off", rg.escalation_reason == "CONTRADICTORY_DATA")
    labour_case = get_case(db, rl.case_id or "")
    b.check("missing labour evidence hand-off names what is missing", rl.escalation_reason == "INCOMPLETE_FEASIBILITY_DATA" and
            labour_case is not None and "labour_availability" in labour_case.context.get("missing", []))
    opened = list_open_cases(db)
    b.check("every hand-off is an open queue case with full context", len(opened) == 3 and all(
        {"language", "tools_executed", "recent_turns", "selected_shipment_id"} <= set(c.context) for c in opened))
    b.say("\n           coordinator queue:")
    b.transcript.extend("           " + line for line in render_queue(opened).splitlines())

    # 8. coordinator works the no-slot case
    b.say("\n-- 8. A coordinator claims and resolves the no-slot case --")
    clock.set(at("09:50").replace(second=0))
    cid = rn.case_id or ""
    claimed = claim_case(db, cid, "asha", clock)
    second = claim_case(db, cid, "ben", clock)
    resolved = resolve_case(db, cid, "asha", "ASSIGNED_SLOT", "We booked the heavy-duty dock at 14:00 for you.", clock)
    b.say(f"           asha claim: {claimed.status}; ben claim: {second.status} (held by {second.claimed_by}); asha resolve: {resolved.status}")
    for row in sql("select message_text from chat_messages where thread_id='THR004' and sender_type='OPERATIONS' order by rowid"):
        b.say(f"           [driver chat, OPERATIONS] {row[0]}")
    b.check("coordinator lifecycle: one owner, resolution reaches the driver", claimed.status == "CLAIMED" and second.status == "ALREADY_CLAIMED" and
            resolved.status == "RESOLVED" and sql("select count(*) from chat_messages where thread_id='THR004' and sender_type='OPERATIONS'") == [(2,)])

    # 9. downstream: appointment history and notification state
    b.say("\n-- 9. Downstream parties: appointment history and notification state --")
    for sid in ("SHP1006", "SHP1017"):
        for e in get_appointment_history(db, sid):
            b.say(f"           {sid} #{e['event_id']:<2} {e['event_type']:<22} {e['appointment_id']:<14} {e['from_status'] or '-':>9} -> {e['to_status'] or '-':<9} "
                  f"{e['actor_type']}/{e['actor_id'] or '-'}  {e['reason']}")
    attempts = {"n": 0}

    def sender(n: Notice) -> None:
        attempts["n"] += 1
        if n.recipient_type == "CONSIGNEE" and attempts["n"] <= 3:
            raise ConnectionError("consignee mail relay unavailable")

    for run in (1, 2):
        d = deliver_pending_notifications(db, clock, sender)
        b.say(f"           delivery run {run}: sent={d.sent} failed={d.failed} permanent={d.permanent_failures}")
    outbox = sql("select recipient_type, substr(dedupe_key,1,instr(dedupe_key,':')-1), status, attempts from notification_outbox order by outbox_id")
    for row in outbox:
        b.say(f"           outbox  {row[0]:<18} {row[1]:<22} {row[2]:<10} attempts={row[3]}")
    b.check("downstream notices queued for planner, consignee and operations on confirm and on cancel",
            {r[0] for r in outbox} >= {"WAREHOUSE_PLANNER", "CONSIGNEE", "OPERATIONS", "DRIVER"})
    b.check("notification failure left the booking intact and a retry delivered it",
            all(r[2] == "SENT" for r in outbox) and any(r[3] == 2 for r in outbox) and
            sql("select appointment_status from appointments where appointment_id=(select appointment_id from appointment_events where event_type='RESCHEDULE_CONFIRMED' limit 1)") == [("CONFIRMED",)])
    b.check("history shows who changed what and why", any(e["event_type"] == "ORIGINAL_ENDED" and e["actor_id"] == "DRV006" for e in get_appointment_history(db, "SHP1006")))

    # integrity, evaluators, scorecard
    violations = check_invariants(db)
    b.say(f"\n-- integrity: check_invariants() -> {violations} --")
    b.check("zero invariant violations at the end", violations == [])
    traces = [c.trace() for c in chats.values()]
    findings = [f for t in traces for f in run_evaluators(t)]
    by_eval: dict[str, list[bool]] = {}
    for f in findings:
        by_eval.setdefault(f.evaluator, []).append(f.ok)
    b.check("every deterministic evaluator passes on every driver's trace", all(f.ok for f in findings), f"{sum(f.ok for f in findings)}/{len(findings)}")
    handoffs = [t for tr in traces for t in tr.turns if t.reply_status == "HANDOFF"]
    claims = [t for tr in traces for t in tr.turns if "confirmed" in t.reply_text.lower() and t.verified_confirmed]
    drivers = list(chats)
    auto = {"DRV006": r6c.verified_confirmed, "DRV001": bool(rc.text.startswith("Your appointment has been cancelled"))}
    b.scorecard = {
        "conflicting_allocations": {"numerator": len(violations), "denominator": 1, "note": "invariant violations at the end; the contested slot has exactly one hold"},
        "cross_driver_reads": {"attempted": 1, "refused": 1, "succeeded": 0},
        "state_backed_confirmations": {"numerator": len(claims), "denominator": len(claims), "note": "confirmation replies that cite a committed CONFIRMED row"},
        "complete_handoffs": {"numerator": sum(1 for c in opened if {"language", "tools_executed", "recent_turns", "selected_shipment_id"} <= set(c.context)), "denominator": len(opened)},
        "evaluator_checks": {"passed": sum(f.ok for f in findings), "total": len(findings), "by_evaluator": {k: f"{sum(v)}/{len(v)}" for k, v in by_eval.items()}},
        "drivers": len(drivers),
        "resolved_without_a_person": {"numerator": sum(auto.values()), "denominator": len(drivers), "note": "deliberately a hard morning; not a test of the 80% target"},
        "handed_to_operations": len(handoffs),
        "failure_classes": {t.turn: classify_failure(t) for tr in traces for t in tr.turns if classify_failure(t)},
    }
    db.close()
    return b
