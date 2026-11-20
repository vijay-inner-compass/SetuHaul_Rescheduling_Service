"""Phase 6 evidence: idempotency, cancellation transcripts, appointment history, outbox states."""

# Lazy annotations.
from __future__ import annotations

# sys.path setup.
import sys

# Scratch database folder.
import tempfile

# Path arithmetic.
from pathlib import Path

# Make tests helpers importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Make src/ importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Test helpers.
from tests.helpers import at, eta_at, seed_everything_known, show_options

# Module access, to inject a failure with its fault hook.
from setuhaul import booking

# Hold, confirm and wording functions.
from setuhaul.booking import (
    ConfirmCommand,
    acquire_hold,
    confirm_reschedule,
    render_confirmation_text,
)

# Never-mind, cancel and return-to-original.
from setuhaul.cancellation import (
    cancel_confirmed_appointment,
    cancel_pending_change,
    request_original_slot_again,
)

# Fixed clock.
from setuhaul.clock import FakeClock

# Database helpers.
from setuhaul.db import make_working_db, open_connection

# Appointment history reader.
from setuhaul.events import get_appointment_history

# Outbox delivery.
from setuhaul.notifications import Notice, PermanentDeliveryError, deliver_pending_notifications

# A fresh working database.
db = open_connection(path=make_working_db(Path(tempfile.mkdtemp()) / "demo.sqlite3"))
# Standard test evidence.
seed_everything_known(db)
# 09:35 on the snapshot day.
clock = FakeClock(at("09:35"))


# Build a confirm command, overriding fields to create mismatch cases.
def cmd(held, mid="MSG-1", **kw):  # type: ignore[no-untyped-def]
    # Default fields.
    base = {"driver_id": "DRV006", "thread_id": "THR001", "message_id": mid, "shipment_id": "SHP1006",
                "hold_appointment_id": held.appointment_id}
    # The command.
    return ConfirmCommand(**{**base, **kw})


# Part 1: 'never mind' releases the hold and leaves the original appointment untouched.
print("== 1. 'Never mind, keep my original slot' before confirming ==")
# Show options.
o = show_options(db, clock)
# Hold option 1.
h = acquire_hold(db, "DRV006", "THR001", "SHP1006", o.option_set_id, 1, clock)
# The hold exists; the original is still CONFIRMED.
print(f"  hold {h.appointment_id} on {h.slot_id}; APT1006 =",
      db.execute("select appointment_status from appointments where appointment_id='APT1006'").fetchone()[0])
# Release it.
print("  cancel_pending_change ->", cancel_pending_change(db, "DRV006", "THR001", "SHP1006", clock).model_dump())
# The original is unchanged.
print("  APT1006 afterwards =", db.execute("select appointment_status, is_current from appointments "
                                           "where appointment_id='APT1006'").fetchone()[:])

# Part 2: confirmation and idempotency.
print("\n== 2. Confirmation and idempotency ==")
o = show_options(db, clock)
h = acquire_hold(db, "DRV006", "THR001", "SHP1006", o.option_set_id, 1, clock)
# Confirm.
r1 = confirm_reschedule(db, cmd(h), clock)
# First answer: CONFIRMED.
print(f"  first : {r1.status} replayed={r1.replayed} appt={r1.appointment_id}")
# The same message again returns the stored answer without acting twice.
print(f"  retry : {confirm_reschedule(db, cmd(h), clock).status} replayed="
      f"{confirm_reschedule(db, cmd(h), clock).replayed} (reply lost, same message id and parameters)")
# The same message id but a different hold.
bad = confirm_reschedule(db, cmd(h, hold_appointment_id='APT-H-SOMETHING-ELSE'), clock)
# Refused as an idempotency mismatch.
print(f"  same message id, different hold -> {bad.status} ({bad.outcome})")
# A new message for an already confirmed hold: ALREADY_CONFIRMED.
print(f"  new message id, same hold       -> {confirm_reschedule(db, cmd(h, 'MSG-2'), clock).status}")
# The confirmation wording comes from the committed row.
print("  text from committed row:", render_confirmation_text(db, h.appointment_id))
# The original was ended by the confirmation.
print("  original APT1006 =", db.execute("select appointment_status, is_current, cancellation_reason from "
                                         "appointments where appointment_id='APT1006'").fetchone()[:])

# Part 3: a failure injected mid-confirmation must roll everything back.
print("\n== 3. Injected failure at AFTER_OUTBOX (new shipment SHP1021) ==")
# Options for a second shipment.
o2 = show_options(db, clock, "THR001", "SHP1021", "11:20")
# Hold one.
h2 = acquire_hold(db, "DRV006", "THR001", "SHP1021", o2.option_set_id, 1, clock)
# Make the confirmation fail after the outbox step.
booking.fault_hook = lambda p: (_ for _ in ()).throw(RuntimeError(p)) if p == "AFTER_OUTBOX" else None
# Try to confirm.
r = confirm_reschedule(db, cmd(h2, "MSG-3", shipment_id="SHP1021"), clock)
# Remove the fault.
booking.fault_hook = None
# SYSTEM_ERROR: the hold is still HELD and no outbox row was written.
print(f"  {r.status} ({r.outcome}); hold still", db.execute("select appointment_status from appointments "
      "where appointment_id=?", (h2.appointment_id,)).fetchone()[0],
      "; outbox rows:", db.execute("select count(*) from notification_outbox").fetchone()[0])

# Clean up: release the hold.
cancel_pending_change(db, "DRV006", "THR001", "SHP1021", clock)
# No holds remain.
print("  hold on SHP1021 released; held rows now:", db.execute("select count(*) from appointments where appointment_status='HELD'").fetchone()[0])

# Part 4: going back to the original slot is a NEW request.
print("\n== 4. Reverting to the original slot after confirmation = a new request ==")
# A query listing the shipment's appointments.
q = "select appointment_id, appointment_status, is_current from appointments where shipment_id='SHP1006' order by 1"
# State before.
before = [tuple(r) for r in db.execute(q)]
# Ask to return to the old slot.
out = request_original_slot_again(db, "DRV006", "THR001", "SHP1006", eta_at("09:50"), clock)
# It is offered as an option, not restored.
print(f"  {out.status}; offered {[i.slot_id for i in out.option_set.items] if out.option_set else None}")
# Nothing was changed or held.
print("  appointment rows unchanged, nothing held:", before == [tuple(r) for r in db.execute(q)])

# Part 5: cancelling a confirmed appointment.
print("\n== 5. Cancelling a confirmed appointment ==")
# 145 minutes away: outside the 120-minute cut-off, so the rules are checked.
print("  SHP1006, slot 12:00, now 09:35 (145 min away):",
      cancel_confirmed_appointment(db, "DRV006", "THR001", "MSG-C1", "SHP1006", clock).model_dump(exclude={"outcome", "replayed"}))
# A thread for another driver.
db.execute("insert into chat_threads (thread_id, driver_id, shipment_id, opened_at, thread_status, thread_intent) "
           "values ('THR-D1','DRV001','SHP1017','2026-08-04T09:00:00+05:30','OPEN','UNKNOWN')")
# Save it.
db.commit()
# Move to 30 minutes before that driver's slot.
clock.set(at("11:30"))
# Try to cancel.
res = cancel_confirmed_appointment(db, "DRV001", "THR-D1", "MSG-C2", "SHP1017", clock)
# Inside the cut-off: ESCALATE, and the appointment stays.
print("  SHP1017, slot 12:00, now 11:30 (30 min away):", res.status, res.escalation_code,
      "| APT1017 =", db.execute("select appointment_status from appointments where appointment_id='APT1017'").fetchone()[0])

# Part 6: the full history of one shipment.
print("\n== 6. Appointment history (SHP1006) ==")
# Each event.
for e in get_appointment_history(db, "SHP1006"):
    # Who changed what, when and why.
    print(f"  #{e['event_id']:>2} {e['event_type']:<22} {e['appointment_id']:<16} {e['from_status'] or '-':>9} -> "
          f"{e['to_status']:<9} by {e['actor_type']}/{e['actor_id']} reason={e['reason']}")

# Part 7: notification outbox retries.
print("\n== 7. Notification outbox states ==")
# A counter for the fake receiver.
state = {"n": 0}


# A fake receiver: the consignee fails three times, operations is permanently rejected for cancellations.
def sender(n: Notice) -> None:
    # Count the call.
    state["n"] += 1
    # Transient failure.
    if n.recipient_type == "CONSIGNEE" and state["n"] <= 3:
        # 'smtp down'.
        raise ConnectionError("smtp down")
    # A permanent refusal.
    if n.recipient_type == "OPERATIONS" and "CANCELLED" in n.dedupe_key:
        # Never retried.
        raise PermanentDeliveryError("mailbox does not exist")


# Two delivery runs.
for round_ in (1, 2):
    # Deliver.
    d = deliver_pending_notifications(db, clock, sender)
    # Sent, failed and permanent counts.
    print(f"  delivery run {round_}: sent={d.sent} failed={d.failed} permanent={d.permanent_failures}")
# Final status of each notice.
for row in db.execute("select recipient_type, substr(dedupe_key,1,28) k, status, attempts from notification_outbox "
                      "order by outbox_id"):
    # Recipient, key, status and attempts.
    print(f"  {row['recipient_type']:<18} {row['k']:<30} {row['status']:<18} attempts={row['attempts']}")
