"""Phase 5 evidence: shared index, external-writer conflict, FCFS race and a fake-clock expiry replay."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tests.helpers import at, seed_everything_known, show_options

from setuhaul.booking import acquire_hold, get_hold
from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection
from setuhaul.jobs.expire_holds import expire_due_holds

db = open_connection(path=make_working_db(Path(tempfile.mkdtemp()) / "demo.sqlite3"))
seed_everything_known(db)
clock = FakeClock(at("09:35"))

print("== 1. Capacity boundary: index definitions after migration 002 ==")
for r in db.execute("select name, sql from sqlite_master where type='index' and name like 'ux_%' order by name"):
    print(" ", " ".join(r["sql"].split()))

a = show_options(db, clock, "THR001", "SHP1006", "11:20")
b = show_options(db, clock, "THR002", "SHP1012", "11:10")
slot = a.items[0].slot_id
print(f"\n== 2. Two valid requests for {slot} (both were shown it as option 1) ==")
r1 = acquire_hold(db, "DRV006", "THR001", "SHP1006", a.option_set_id, 1, clock)
print(f"  DRV006 SHP1006 -> {r1.status} ({r1.outcome}) appt={r1.appointment_id} expires={r1.expires_at:%H:%M:%S}")
r2 = acquire_hold(db, "DRV012", "THR002", "SHP1012", b.option_set_id, 1, clock)
print(f"  DRV012 SHP1012 -> {r2.status} ({r2.outcome}) diff={r2.diff.change if r2.diff else None}")
print("  original appointment APT1006:", tuple(db.execute(
    "select appointment_status, is_current, slot_id from appointments where appointment_id='APT1006'").fetchone()))

print("\n== 3. External scheduler inserts straight into the held slot ==")
try:
    db.execute(
        "insert into appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source, "
        "is_current, booked_at, updated_at) values ('EXT-1','SHP1018',?, 'CONFIRMED','SCHEDULING_TOOL',1, "
        "'2026-08-04T09:36:00+05:30','2026-08-04T09:36:00+05:30')", (slot,))
except sqlite3.IntegrityError as exc:
    print("  rejected by the database:", exc)
db.rollback()

print("\n== 4. One shipment, second slot ==")
r3 = acquire_hold(db, "DRV006", "THR001", "SHP1006", a.option_set_id, 2, clock)
print(f"  {r3.status} (existing hold {r3.existing_appointment_id})")

print("\n== 5. Fake-clock expiry replay (TTL 180 s, no sleeping) ==")
for label, delta in [("t+179s", 179), ("t+180s (exactly at expiry)", 1), ("t+400s", 220)]:
    clock.advance(timedelta(seconds=delta))
    swept = expire_due_holds(db, clock)
    view = get_hold(db, r1.appointment_id, clock)
    print(f"  {label}: swept={swept.expired} hold_state={view.state if view else None}")
print("  repeat sweep:", expire_due_holds(db, clock).expired, "(idempotent)")
ev = [tuple(r) for r in db.execute(
    "select event_type, from_status, to_status, actor_type from appointment_events "
    "where appointment_id=? order by event_id", (r1.appointment_id,))]
print("  history:", ev)
box = db.execute("select recipient_type, recipient_ref, status, payload_json from notification_outbox").fetchone()
print(f"  outbox: {box['recipient_type']} {box['recipient_ref']} {box['status']} -> {json.loads(box['payload_json'])['text']}")
print("  original appointment APT1006:", tuple(db.execute(
    "select appointment_status, is_current, slot_id from appointments where appointment_id='APT1006'").fetchone()))
c = show_options(db, clock, "THR002", "SHP1012", "11:10")
print(f"  slot {slot} offered again to SHP1012: {c.items[0].slot_id == slot}")
