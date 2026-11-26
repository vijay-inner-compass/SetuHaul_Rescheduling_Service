"""Regenerate the evidence behind the submission documents. Reads the snapshot; writes only to temp copies.

Writes docs/verification/data-issues.md (each data issue with its query and result) and
docs/verification/decision-evidence.md (what the real services do for each of the brief's decisions).
Driver messages are not interpreted by a model here: every case calls the deterministic layers directly.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tests.helpers import at, eta_at, seed_everything_known, show_options

from setuhaul.booking import ConfirmCommand, acquire_hold, confirm_reschedule
from setuhaul.capacity import build_facility_state
from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection
from setuhaul.escalation import HandoffRequest, create_handoff, get_case
from setuhaul.eta import derive_eta, load_eta_records
from setuhaul.feasibility import build_context, search_feasible_slots
from setuhaul.language import say
from setuhaul.option_history import reconcile_option_set
from setuhaul.schemas import DelayReport

SOURCE = ROOT / "data" / "setuhaul.sqlite3"
OUT = ROOT / "docs" / "verification"
ACTIVE = "('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')"

ISSUES: list[tuple[str, str, str]] = [
    ("Two active appointments put a load on a dock that cannot take its weight",
     ("SHP1005 (20,500 kg) is on D3 and SHP1014 (21,000 kg) is on D1; both docks are limited to 20,000 kg. "
     "The two unique indexes do not check this."),
     f"""select a.appointment_id, a.shipment_id, a.appointment_status, s.load_weight_kg, d.dock_code, d.max_vehicle_weight_kg
from appointments a join shipments s using(shipment_id) join appointment_slots sl using(slot_id)
join docks d on d.dock_id=sl.dock_id
where a.appointment_status in {ACTIVE} and s.load_weight_kg > d.max_vehicle_weight_kg"""),
    ("Current appointments that the latest ETA can no longer meet",
     "The booking still looks current although the truck is due after the slot starts.",
     """select a.shipment_id, a.appointment_status, sl.slot_start_ts, s.latest_eta_ts
from appointments a join shipments s using(shipment_id) join appointment_slots sl using(slot_id)
where a.is_current=1 and a.appointment_status in ('PENDING_CONFIRMATION','CONFIRMED')
  and julianday(s.latest_eta_ts) > julianday(sl.slot_start_ts) order by a.shipment_id"""),
    ("Unload time longer than the booked slot",
     "A 70 or 75 minute unload in a 60 minute slot runs into the next booking on that dock.",
     f"""select a.shipment_id, a.appointment_status, s.expected_unload_min,
cast(round((julianday(sl.slot_end_ts)-julianday(sl.slot_start_ts))*1440) as int) as slot_min
from appointments a join shipments s using(shipment_id) join appointment_slots sl using(slot_id)
where a.appointment_status in {ACTIVE}
  and s.expected_unload_min > round((julianday(sl.slot_end_ts)-julianday(sl.slot_start_ts))*1440)"""),
    ("Active appointments inside a dock event",
     ("A breakdown or capacity reduction overlaps the booked time, and the appointment is unchanged. "
     "A capacity reduction states no factor, so its real effect is unknown."),
     f"""select a.appointment_id, a.shipment_id, d.dock_code, e.event_type, sl.slot_start_ts, e.event_end_ts
from appointments a join appointment_slots sl using(slot_id) join docks d on d.dock_id=sl.dock_id
join dock_status_events e on e.dock_id=sl.dock_id
 and julianday(sl.slot_start_ts) < julianday(coalesce(e.event_end_ts,'9999-01-01'))
 and julianday(sl.slot_end_ts) > julianday(e.event_start_ts)
where a.appointment_status in {ACTIVE}"""),
    ("A rule value and its description need confirming",
     ("The heavy-dock threshold is 25,000 kg, yet every standard dock is limited to 25,000 kg or less, so loads between "
     "20,000 and 25,000 kg fit only some standard docks."),
     """select 'rule' as kind, rule_type as name, rule_value as value from facility_rules where rule_type='HEAVY_DOCK_REQUIRED_KG'
union all select 'dock', dock_code, max_vehicle_weight_kg from docks where facility_id='FAC-JAI-01' and dock_type='STANDARD'"""),
    ("Open shipments with no active appointment",
     "A driver on these loads has nothing to reschedule from.",
     f"""select s.shipment_id, s.current_status from shipments s where s.current_status not in ('COMPLETED','CANCELLED')
and not exists (select 1 from appointments a where a.shipment_id=s.shipment_id and a.is_current=1
and a.appointment_status in {ACTIVE})"""),
    ("Drivers with two open shipments",
     "A message cannot be tied to a load from the sender alone.",
     """select driver_id, count(*) as open_shipments, group_concat(shipment_id) as shipments from shipments
where current_status not in ('COMPLETED','CANCELLED') group by driver_id having count(*)>1"""),
    ("An exception with no shipment and a duplicate exception",
     "Not every exception maps to a load, and a messaging retry created a second record.",
     """select exception_id, shipment_id, driver_id, exception_status from driver_exceptions
where shipment_id is null or exception_status='DUPLICATE'"""),
    ("A duplicate chat message",
     "Chat channels repeat messages.",
     "select chat_message_id, thread_id from chat_messages where is_duplicate=1"),
    ("Operational messages that did not get through",
     "The night-shift request for the reefer exception FAILED and one slot request is still QUEUED.",
     """select message_id as id, shipment_id, subject, delivery_status from operational_messages
where delivery_status in ('FAILED','QUEUED')"""),
]


def table(cursor: sqlite3.Cursor) -> list[str]:
    heads = [c[0] for c in cursor.description]
    rows = cursor.fetchall()
    out = ["| " + " | ".join(heads) + " |", "|" + "---|" * len(heads)]
    out += ["| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in rows]
    return out + ["", f"{len(rows)} row(s)."]


def data_issues() -> str:
    conn = sqlite3.connect(f"file:{SOURCE.as_posix()}?mode=ro", uri=True)
    names = [r[1] for r in conn.execute("pragma table_info(operational_messages)")]
    lines = ["# Data issues, each with its query", "",
             "Generated by `python scripts/submission_evidence.py` against the supplied snapshot, opened read-only.", ""]
    for number, (title, why, query) in enumerate(ISSUES, 1):
        query = query.replace("message_id as id", f"{names[0]} as id")
        lines += [f"## {number}. {title}", "", why, "", "```sql", query, "```", ""]
        lines += table(conn.execute(query)) + [""]
    conn.close()
    return "\n".join(lines)


def world(work: Path, name: str, seeded: bool = True) -> sqlite3.Connection:
    conn = open_connection(path=make_working_db(work / f"{name}.sqlite3"))
    if seeded:
        seed_everything_known(conn)
    return conn


def eta_line(conn: sqlite3.Connection, shipment: str, report: DelayReport | None, clock: FakeClock) -> str:
    planned, current = load_eta_records(conn, shipment)
    result: Any = derive_eta(report, planned, current, None, clock)
    if hasattr(result, "eta"):
        return (f"ETA {result.eta:%H:%M}, source {result.source}, confidence {result.confidence}, "
                f"baseline {result.baseline_type.value}")
    return f"asks ({result.code}): \"{result.question}\""


def decision_evidence() -> str:
    clock = FakeClock(at("09:35"))
    out = ["# Evidence for the 13 decisions", "",
           ("Generated by `python scripts/submission_evidence.py`. Clock fixed at 2026-08-04 09:35 IST on temporary copies "
           "of the snapshot. Labour and consignee rows are `TEST_FIXTURE` unless a case says otherwise. No model is used: "
           "these are the results of the deterministic services."), ""]
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        work = Path(tmp)
        conn = world(work, "base")

        out += ["## E1. What is asked before options are shown (three seed chats)", "",
                "| Seed chat | Driver message | What the ETA rule does |", "|---|---|---|"]
        for thread, shipment, report in (
            ("THR001", "SHP1006", DelayReport.explicit(at("11:20"))),
            ("THR003", "SHP1013", DelayReport.late_by(60)),
            (None, "SHP1012", None),
            (None, "SHP1012", DelayReport.repair(90)),
        ):
            if thread:
                text = conn.execute("select message_text from chat_messages where thread_id=? and sender_type='DRIVER' "
                                    "order by message_ts limit 1", (thread,)).fetchone()[0]
            else:
                text = "(constructed) The repair will take 90 minutes." if report else "(constructed) I will be late."
            out.append(f"| {thread or '-'}, {shipment} | {text} | {eta_line(conn, shipment, report, clock)} |")

        out += ["", "## E2. Message to rows, unclear case (THR010, DRV004)", "", "```sql",
                "-- the message and its exception record (no shipment)",
                "select thread_id, driver_id, shipment_id from chat_threads where thread_id='THR010';",
                "```", ""]
        out += table(conn.execute("select thread_id, driver_id, shipment_id from chat_threads where thread_id='THR010'"))
        out += [""] + table(conn.execute("select exception_id, shipment_id, exception_status, description from "
                                         "driver_exceptions where thread_id='THR010'"))
        out += ["", "The driver's open loads and their current appointments:", ""]
        out += table(conn.execute(
            "select s.shipment_id, s.destination_facility_id, a.appointment_id, a.appointment_status, sl.slot_start_ts "
            "from shipments s left join appointments a on a.shipment_id=s.shipment_id and a.is_current=1 "
            "left join appointment_slots sl using(slot_id) where s.driver_id='DRV004' and s.current_status not in "
            "('COMPLETED','CANCELLED') order by s.shipment_id"))

        out += ["", "## E3. The ETA rule on two messages", "",
                f"* SHP1006, \"60 minutes late\": {eta_line(conn, 'SHP1006', DelayReport.late_by(60), clock)}",
                f"* SHP1006, \"repair takes 90 minutes\": {eta_line(conn, 'SHP1006', DelayReport.repair(90), clock)}",
                (f"* ...then \"the drive is 45 minutes\": "
                f"{eta_line(conn, 'SHP1006', DelayReport.repair(90, travel_minutes=45), clock)}")]

        out += ["", "## E4. The feasibility check on three shipments", "",
                "| Shipment | ETA | Evidence | Result | Feasible slots | Rejections (slot counts) | Missing |",
                "|---|---|---|---|---|---|---|"]
        bare = world(work, "bare", seeded=False)
        for db, label in ((conn, "TEST_FIXTURE"), (bare, "snapshot only")):
            for shipment, eta in (("SHP1006", "11:20"), ("SHP1014", "11:25"), ("SHP1015", "18:30")):
                ctx = build_context(db, shipment, eta_at(eta), clock.now())
                res = search_feasible_slots(ctx, build_facility_state(db, ctx.facility_id, clock.now()))
                missing = sorted({m for d in res.incomplete for m in d.missing})
                rejected = ", ".join(f"{k} {v}" for k, v in sorted(res.rejection_counts.items()))
                out.append(f"| {shipment} | {eta} | {label} | {res.status} | {len(res.feasible)} | {rejected} | "
                           f"{', '.join(missing)} |")
        bare.close()

        out += ["", "## E7. Two trucks, one slot: the ordering policy", ""]
        for first, second in ((("DRV006", "THR001", "SHP1006"), ("DRV012", "THR002", "SHP1012")),
                              (("DRV012", "THR002", "SHP1012"), ("DRV006", "THR001", "SHP1006"))):
            db = world(work, f"race-{first[2]}-{second[2]}")
            results = []
            sets = [show_options(db, clock, thread=t, shipment=s, eta="11:20") for _, t, s in (first, second)]
            for (driver, thread, shipment), option_set in zip((first, second), sets, strict=True):
                r = acquire_hold(db, driver, thread, shipment, option_set.option_set_id, 1, clock)
                priority = db.execute("select priority_code from shipments where shipment_id=?", (shipment,)).fetchone()[0]
                results.append(f"{shipment} ({priority}): {r.status}")
            out.append(f"* Commit order {first[2]} then {second[2]}, both asking for their first option: "
                       f"{'; '.join(results)}")
            db.close()

        out += ["", "## E9. Stale options, retries and duplicates", ""]
        db = world(work, "stale")
        shown = show_options(db, clock)
        taken = shown.items[0].slot_id
        db.execute("insert into appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source, "
                   "is_current, booked_at, updated_at) values ('PLAN-1','SHP1018',?,'CONFIRMED','PLANNER',1,?,?)",
                   (taken, clock.now().isoformat(), clock.now().isoformat()))
        db.commit()
        diff = reconcile_option_set(db, shown, clock)
        out.append(f"* Stale option: a planner books option 1 after it was shown. Comparison: "
                   f"{[(i.rank, i.change) for i in diff.items]}")
        held = acquire_hold(db, "DRV006", "THR001", "SHP1006", shown.option_set_id, 1, clock)
        out.append(f"* Choosing that stale option: {held.status}")
        held = acquire_hold(db, "DRV006", "THR001", "SHP1006", shown.option_set_id, 2, clock)
        cmd = ConfirmCommand(driver_id="DRV006", thread_id="THR001", message_id="MSG-1", shipment_id="SHP1006",
                             hold_appointment_id=held.appointment_id)
        one, two = confirm_reschedule(db, cmd, clock), confirm_reschedule(db, cmd, clock)
        out.append(f"* Confirm, then the same message retried: {one.status} (replayed={one.replayed}), "
                   f"{two.status} (replayed={two.replayed})")
        count = db.execute("select count(*) from appointments where shipment_id='SHP1006' and appointment_status="
                           "'CONFIRMED'").fetchone()[0]
        out.append(f"* Confirmed appointments for SHP1006 after the retry: {count}")
        out.append("* Duplicate inbound message: "
                   "`tests/test_api.py::test_duplicate_delivery_returns_the_stored_reply_without_another_model_call`")
        out.append("* Cancellation: `tests/test_cancellation.py::test_cancel_before_cutoff_cancels_transactionally`, "
                   "`test_cancel_is_idempotent_and_detects_mismatch`; also step 6 of the scenario run.")

        out += ["", "## E10. No feasible slot: message and hand-off record", ""]
        req = HandoffRequest(reason_code="NO_SLOT", driver_id="DRV014", thread_id="THR004", shipment_id="SHP1014",
                             summary="No slot can be offered automatically (NO_SLOT).", language="en",
                             created_at=clock.now(), context={"eta": "11:25"})
        made = create_handoff(db, req, clock)
        case = get_case(db, made.case_id)
        out += [f"Driver hears: \"{say('handoff', 'en', case=made.case_id)}\"", "", "Stored case:", "", "```json",
                case.model_dump_json(indent=2), "```", "",
                ("A case raised from a live conversation also carries the recent turns and the tools run "
                "(scenario check 17).")]
        db.close()

        out += ["", "## E11. Refusal messages, as the driver reads them", "",
                f"* Hold lapsed: \"{say('hold_expired', 'en')}\"",
                f"* Truck can no longer reach the slot: \"{say('position_changed', 'en')}\"",
                f"* Service cannot check bookings: \"{say('temp_unavailable', 'en')}\""]
        conn.close()
    return "\n".join(out) + "\n"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "data-issues.md").write_text(data_issues(), encoding="utf-8", newline="\n")
    (OUT / "decision-evidence.md").write_text(decision_evidence(), encoding="utf-8", newline="\n")
    print("wrote docs/verification/data-issues.md and decision-evidence.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
