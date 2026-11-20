"""Phase 5 evidence: shared index, external-writer conflict, FCFS race and a fake-clock expiry replay."""

# Lazy annotations.
from __future__ import annotations

# Decode the outbox payload.
import json

# Catch the database's integrity error.
import sqlite3

# sys.path setup.
import sys

# Scratch database folder.
import tempfile

# Time steps.
from datetime import timedelta

# Path arithmetic.
from pathlib import Path

# Make tests helpers importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Make src/ importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Helpers to seed evidence and show options.
from tests.helpers import at, seed_everything_known, show_options

# Hold operations.
from setuhaul.booking import acquire_hold, get_hold

# Fixed clock.
from setuhaul.clock import FakeClock

# Database helpers.
from setuhaul.db import make_working_db, open_connection

# The expiry sweep.
from setuhaul.jobs.expire_holds import expire_due_holds

# A fresh working database.
db = open_connection(path=make_working_db(Path(tempfile.mkdtemp()) / "demo.sqlite3"))
# Standard test evidence.
seed_everything_known(db)
# 09:35 on the snapshot day.
clock = FakeClock(at("09:35"))

# Part 1: the unique indexes that make double booking impossible.
print("== 1. Capacity boundary: index definitions after migration 002 ==")
# Print each unique index definition.
for r in db.execute("select name, sql from sqlite_master where type='index' and name like 'ux_%' order by name"):
    print(" ", " ".join(r["sql"].split()))

# Show SHP1006 its options.
a = show_options(db, clock, "THR001", "SHP1006", "11:20")
# Show SHP1012 its options. Both see the same best slot.
b = show_options(db, clock, "THR002", "SHP1012", "11:10")
# The contested slot.
slot = a.items[0].slot_id
# Part 2: two valid requests for the same slot.
print(f"\n== 2. Two valid requests for {slot} (both were shown it as option 1) ==")
# The first driver holds it.
r1 = acquire_hold(db, "DRV006", "THR001", "SHP1006", a.option_set_id, 1, clock)
# HELD.
print(f"  DRV006 SHP1006 -> {r1.status} ({r1.outcome}) appt={r1.appointment_id} expires={r1.expires_at:%H:%M:%S}")
# The second driver tries.
r2 = acquire_hold(db, "DRV012", "THR002", "SHP1012", b.option_set_id, 1, clock)
# SLOT_CONFLICT: first to commit wins.
print(f"  DRV012 SHP1012 -> {r2.status} ({r2.outcome}) diff={r2.diff.change if r2.diff else None}")
# The first driver's original appointment is untouched by the hold.
print("  original appointment APT1006:", tuple(db.execute(
    "select appointment_status, is_current, slot_id from appointments where appointment_id='APT1006'").fetchone()))

# Part 3: another system writing directly to the database.
print("\n== 3. External scheduler inserts straight into the held slot ==")
# Insert straight into the held slot.
try:
    db.execute(
        "insert into appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source, "
        "is_current, booked_at, updated_at) values ('EXT-1','SHP1018',?, 'CONFIRMED','SCHEDULING_TOOL',1, "
        "'2026-08-04T09:36:00+05:30','2026-08-04T09:36:00+05:30')", (slot,))
# The database itself refuses: the hold is inside the same unique index.
except sqlite3.IntegrityError as exc:
    print("  rejected by the database:", exc)
# Discard the failed attempt.
db.rollback()

# Part 4: one hold per shipment.
print("\n== 4. One shipment, second slot ==")
# The same driver asks for a second slot.
r3 = acquire_hold(db, "DRV006", "THR001", "SHP1006", a.option_set_id, 2, clock)
# SHIPMENT_ALREADY_HELD.
print(f"  {r3.status} (existing hold {r3.existing_appointment_id})")

# Part 5: expiry without sleeping.
print("\n== 5. Fake-clock expiry replay (TTL 180 s, no sleeping) ==")
# Three moments: just before expiry, exactly at expiry, well after.
for label, delta in [("t+179s", 179), ("t+180s (exactly at expiry)", 1), ("t+400s", 220)]:
    # Move the fake clock.
    clock.advance(timedelta(seconds=delta))
    # Run the sweep.
    swept = expire_due_holds(db, clock)
    # The hold's state.
    view = get_hold(db, r1.appointment_id, clock)
    # Nothing expires at 179 s, it expires at exactly 180 s.
    print(f"  {label}: swept={swept.expired} hold_state={view.state if view else None}")
# Sweeping again does nothing: it is idempotent.
print("  repeat sweep:", expire_due_holds(db, clock).expired, "(idempotent)")
# The hold's history.
ev = [tuple(r) for r in db.execute(
    "select event_type, from_status, to_status, actor_type from appointment_events "
    "where appointment_id=? order by event_id", (r1.appointment_id,))]
# Acquired, then expired by the system.
print("  history:", ev)
# The driver notice queued in the outbox.
box = db.execute("select recipient_type, recipient_ref, status, payload_json from notification_outbox").fetchone()
# Its wording.
print(f"  outbox: {box['recipient_type']} {box['recipient_ref']} {box['status']} -> {json.loads(box['payload_json'])['text']}")
print("  original appointment APT1006:", tuple(db.execute(
    "select appointment_status, is_current, slot_id from appointments where appointment_id='APT1006'").fetchone()))
# Show the options to the other driver again.
c = show_options(db, clock, "THR002", "SHP1012", "11:10")
# The lapsed hold's slot is available again.
print(f"  slot {slot} offered again to SHP1012: {c.items[0].slot_id == slot}")
