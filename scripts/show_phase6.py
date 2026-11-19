"""Phase 6 evidence: idempotency, cancellation transcripts, appointment history, outbox states."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tests.helpers import at, eta_at, seed_everything_known, show_options

from setuhaul import booking
from setuhaul.booking import (
    ConfirmCommand,
    acquire_hold,
    confirm_reschedule,
    render_confirmation_text,
)
from setuhaul.cancellation import (
    cancel_confirmed_appointment,
    cancel_pending_change,
    request_original_slot_again,
)
from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection
from setuhaul.events import get_appointment_history
from setuhaul.notifications import Notice, PermanentDeliveryError, deliver_pending_notifications

db = open_connection(path=make_working_db(Path(tempfile.mkdtemp()) / "demo.sqlite3"))
seed_everything_known(db)
clock = FakeClock(at("09:35"))


def cmd(held, mid="MSG-1", **kw):  # type: ignore[no-untyped-def]
    base = {"driver_id": "DRV006", "thread_id": "THR001", "message_id": mid, "shipment_id": "SHP1006",
                "hold_appointment_id": held.appointment_id}
    return ConfirmCommand(**{**base, **kw})


print("== 1. 'Never mind, keep my original slot' before confirming ==")
o = show_options(db, clock)
h = acquire_hold(db, "DRV006", "THR001", "SHP1006", o.option_set_id, 1, clock)
print(f"  hold {h.appointment_id} on {h.slot_id}; APT1006 =",
      db.execute("select appointment_status from appointments where appointment_id='APT1006'").fetchone()[0])
print("  cancel_pending_change ->", cancel_pending_change(db, "DRV006", "THR001", "SHP1006", clock).model_dump())
print("  APT1006 afterwards =", db.execute("select appointment_status, is_current from appointments "
                                           "where appointment_id='APT1006'").fetchone()[:])

print("\n== 2. Confirmation and idempotency ==")
o = show_options(db, clock)
h = acquire_hold(db, "DRV006", "THR001", "SHP1006", o.option_set_id, 1, clock)
r1 = confirm_reschedule(db, cmd(h), clock)
print(f"  first : {r1.status} replayed={r1.replayed} appt={r1.appointment_id}")
print(f"  retry : {confirm_reschedule(db, cmd(h), clock).status} replayed="
      f"{confirm_reschedule(db, cmd(h), clock).replayed} (reply lost, same message id and parameters)")
bad = confirm_reschedule(db, cmd(h, hold_appointment_id='APT-H-SOMETHING-ELSE'), clock)
print(f"  same message id, different hold -> {bad.status} ({bad.outcome})")
print(f"  new message id, same hold       -> {confirm_reschedule(db, cmd(h, 'MSG-2'), clock).status}")
print("  text from committed row:", render_confirmation_text(db, h.appointment_id))
print("  original APT1006 =", db.execute("select appointment_status, is_current, cancellation_reason from "
                                         "appointments where appointment_id='APT1006'").fetchone()[:])

print("\n== 3. Injected failure at AFTER_OUTBOX (new shipment SHP1021) ==")
o2 = show_options(db, clock, "THR001", "SHP1021", "11:20")
h2 = acquire_hold(db, "DRV006", "THR001", "SHP1021", o2.option_set_id, 1, clock)
booking.fault_hook = lambda p: (_ for _ in ()).throw(RuntimeError(p)) if p == "AFTER_OUTBOX" else None
r = confirm_reschedule(db, cmd(h2, "MSG-3", shipment_id="SHP1021"), clock)
booking.fault_hook = None
print(f"  {r.status} ({r.outcome}); hold still", db.execute("select appointment_status from appointments "
      "where appointment_id=?", (h2.appointment_id,)).fetchone()[0],
      "; outbox rows:", db.execute("select count(*) from notification_outbox").fetchone()[0])

cancel_pending_change(db, "DRV006", "THR001", "SHP1021", clock)
print("  hold on SHP1021 released; held rows now:", db.execute("select count(*) from appointments where appointment_status='HELD'").fetchone()[0])

print("\n== 4. Reverting to the original slot after confirmation = a new request ==")
q = "select appointment_id, appointment_status, is_current from appointments where shipment_id='SHP1006' order by 1"
before = [tuple(r) for r in db.execute(q)]
out = request_original_slot_again(db, "DRV006", "THR001", "SHP1006", eta_at("09:50"), clock)
print(f"  {out.status}; offered {[i.slot_id for i in out.option_set.items] if out.option_set else None}")
print("  appointment rows unchanged, nothing held:", before == [tuple(r) for r in db.execute(q)])

print("\n== 5. Cancelling a confirmed appointment ==")
print("  SHP1006, slot 12:00, now 09:35 (145 min away):",
      cancel_confirmed_appointment(db, "DRV006", "THR001", "MSG-C1", "SHP1006", clock).model_dump(exclude={"outcome", "replayed"}))
db.execute("insert into chat_threads (thread_id, driver_id, shipment_id, opened_at, thread_status, thread_intent) "
           "values ('THR-D1','DRV001','SHP1017','2026-08-04T09:00:00+05:30','OPEN','UNKNOWN')")
db.commit()
clock.set(at("11:30"))
res = cancel_confirmed_appointment(db, "DRV001", "THR-D1", "MSG-C2", "SHP1017", clock)
print("  SHP1017, slot 12:00, now 11:30 (30 min away):", res.status, res.escalation_code,
      "| APT1017 =", db.execute("select appointment_status from appointments where appointment_id='APT1017'").fetchone()[0])

print("\n== 6. Appointment history (SHP1006) ==")
for e in get_appointment_history(db, "SHP1006"):
    print(f"  #{e['event_id']:>2} {e['event_type']:<22} {e['appointment_id']:<16} {e['from_status'] or '-':>9} -> "
          f"{e['to_status']:<9} by {e['actor_type']}/{e['actor_id']} reason={e['reason']}")

print("\n== 7. Notification outbox states ==")
state = {"n": 0}


def sender(n: Notice) -> None:
    state["n"] += 1
    if n.recipient_type == "CONSIGNEE" and state["n"] <= 3:
        raise ConnectionError("smtp down")
    if n.recipient_type == "OPERATIONS" and "CANCELLED" in n.dedupe_key:
        raise PermanentDeliveryError("mailbox does not exist")


for round_ in (1, 2):
    d = deliver_pending_notifications(db, clock, sender)
    print(f"  delivery run {round_}: sent={d.sent} failed={d.failed} permanent={d.permanent_failures}")
for row in db.execute("select recipient_type, substr(dedupe_key,1,28) k, status, attempts from notification_outbox "
                      "order by outbox_id"):
    print(f"  {row['recipient_type']:<18} {row['k']:<30} {row['status']:<18} attempts={row['attempts']}")
