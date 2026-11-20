"""Phase 4 evidence: search outcomes, a stored option set, and a later diff explaining a change."""

# Lazy annotations.
from __future__ import annotations

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

# Test helpers: times, ETA evidence, standard labour and consignee seed.
from tests.helpers import at, eta_at, seed_everything_known

# Ranking and first-come-first-served allocation.
from setuhaul.allocation import allocate_fcfs, rank_options

# Facility snapshot.
from setuhaul.capacity import build_facility_state

# Fixed clock.
from setuhaul.clock import FakeClock

# Database helpers.
from setuhaul.db import make_working_db, open_connection

# Slot search.
from setuhaul.feasibility import build_context, search_feasible_slots

# Stored menus and their comparison.
from setuhaul.option_history import get_latest_option_set, reconcile_option_set, record_option_set

# A fresh working database.
db = open_connection(path=make_working_db(Path(tempfile.mkdtemp()) / "demo.sqlite3"))
# 09:35 on the snapshot day.
clock = FakeClock(at("09:35"))
# Five contested shipments and their arrival times.
ETAS = {"SHP1006": "11:20", "SHP1012": "11:10", "SHP1013": "11:00", "SHP1014": "11:25", "SHP1009": "10:50"}


# Search for one shipment and return its context, state and result.
def search(sid: str):  # type: ignore[no-untyped-def]
    # The feasibility context.
    ctx = build_context(db, sid, eta_at(ETAS[sid]), clock.now())
    # The facility snapshot.
    state = build_facility_state(db, ctx.facility_id, clock.now())
    # Context, state and result.
    return ctx, state, search_feasible_slots(ctx, state)


# Part 1: with the supplied data only, nothing can be offered, because labour and customer evidence is missing.
print("== 1. Snapshot as supplied: no labour or consignee evidence ==")
# Search.
_, _, r = search("SHP1006")
# INCOMPLETE_FEASIBILITY_DATA, not a guess.
print(f"  SHP1006 status={r.status} feasible={len(r.feasible)} incomplete={len(r.incomplete)}")
# What evidence is missing.
print(f"  missing={r.incomplete[0].missing}")

# Add the labelled test evidence.
seed_everything_known(db)  # TEST_FIXTURE labour (8 crews, 06:00-22:00) and 'no deadline' consignee rows
# Part 2: the five contested shipments.
print("\n== 2. With labelled fixture evidence: the five contested shipments ==")
# Ranked options per shipment.
opts = {}
# Search each.
for sid in ETAS:
    ctx, state, r = search(sid)
    # Keep up to six ranked options.
    opts[sid] = rank_options(r.feasible, limit=6)
    # The top three as 'time dock'.
    top = [f"{o.decision.slot_start:%H:%M} {o.decision.dock_code}" for o in opts[sid][:3]]
    # Show weight, unload time, status, top options and the most common rejection reasons.
    print(f"  {sid} ({ctx.unload.load_weight_kg} kg, {ctx.unload.expected_unload_min} min): "
          f"{r.status:8} top={top} rejected={dict(sorted(r.rejection_counts.items(), key=lambda kv: -kv[1])[:3])}")
# Allocate in commit order.
print("\n  FCFS in commit order", list(ETAS), "->")
# Each shipment's slot, or a hand-off if none is left.
for sid, slot in allocate_fcfs([(s, opts[s]) for s in ETAS]).items():
    # Print it.
    print(f"    {sid}: {slot or 'NO SLOT -> hand-off'}")

# Part 3: a stored menu.
print("\n== 3. Stored option set for SHP1006 ==")
# Search again.
ctx, state, r = search("SHP1006")
# Store the menu.
shown = record_option_set(db, ctx, "THR001", rank_options(r.feasible, 3), state, clock)
# Its id, times and rules version.
print(f"  {shown.option_set_id} shown_at={shown.shown_at:%H:%M:%S} expires_at={shown.expires_at:%H:%M:%S} "
      f"rules={shown.rules_version}")
# Each option.
for it in shown.items:
    # Its decision.
    d = it.decision
    # Rank, slot, time, dock and fingerprints.
    print(f"   rank {it.rank}: {d.slot_id} {d.slot_start:%H:%M} {d.dock_code} version={it.slot_version} "
          f"hash={it.feasibility_hash}")

# Part 4: the driver returns and the world has changed.
print("\n== 4. Driver returns 4 minutes later; planner books option 1 for another truck ==")
# Option 1's slot.
taken = shown.items[0].slot_id
# A planner books that slot for another truck.
db.execute(
    "insert into appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source, "
    "is_current, booked_at, updated_at) values ('APT-DEMO','SHP1018',?, 'CONFIRMED','PLANNER',1, "
    "'2026-08-04T09:38:00+05:30','2026-08-04T09:38:00+05:30')", (taken,))
# Save it.
db.commit()
# Four minutes later.
clock.advance(timedelta(minutes=4))
# The menu the driver saw.
latest = get_latest_option_set(db, "DRV006", "THR001", "SHP1006")
# Compare it with reality.
diff = reconcile_option_set(db, latest, clock)  # type: ignore[arg-type]
# Whether the menu expired, rules changed, and which ranks remain.
print(f"  expired={diff.expired} rules_changed={diff.rules_changed} still_offerable_ranks={diff.still_offerable}")
# Each option's change.
for i in diff.items:
    # What changed and the explanation.
    print(f"   rank {i.rank} {i.slot_id}: {i.change} {i.reason_codes} -> {i.explanation}")
