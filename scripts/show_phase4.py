"""Phase 4 evidence: search outcomes, a stored option set, and a later diff explaining a change."""

from __future__ import annotations

import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tests.helpers import at, eta_at, seed_everything_known

from setuhaul.allocation import allocate_fcfs, rank_options
from setuhaul.capacity import build_facility_state
from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection
from setuhaul.feasibility import build_context, search_feasible_slots
from setuhaul.option_history import get_latest_option_set, reconcile_option_set, record_option_set

db = open_connection(path=make_working_db(Path(tempfile.mkdtemp()) / "demo.sqlite3"))
clock = FakeClock(at("09:35"))
ETAS = {"SHP1006": "11:20", "SHP1012": "11:10", "SHP1013": "11:00", "SHP1014": "11:25", "SHP1009": "10:50"}


def search(sid: str):  # type: ignore[no-untyped-def]
    ctx = build_context(db, sid, eta_at(ETAS[sid]), clock.now())
    state = build_facility_state(db, ctx.facility_id, clock.now())
    return ctx, state, search_feasible_slots(ctx, state)


print("== 1. Snapshot as supplied: no labour or consignee evidence ==")
_, _, r = search("SHP1006")
print(f"  SHP1006 status={r.status} feasible={len(r.feasible)} incomplete={len(r.incomplete)}")
print(f"  missing={r.incomplete[0].missing}")

seed_everything_known(db)  # TEST_FIXTURE labour (8 crews, 06:00-22:00) and 'no deadline' consignee rows
print("\n== 2. With labelled fixture evidence: the five contested shipments ==")
opts = {}
for sid in ETAS:
    ctx, state, r = search(sid)
    opts[sid] = rank_options(r.feasible, limit=6)
    top = [f"{o.decision.slot_start:%H:%M} {o.decision.dock_code}" for o in opts[sid][:3]]
    print(f"  {sid} ({ctx.unload.load_weight_kg} kg, {ctx.unload.expected_unload_min} min): "
          f"{r.status:8} top={top} rejected={dict(sorted(r.rejection_counts.items(), key=lambda kv: -kv[1])[:3])}")
print("\n  FCFS in commit order", list(ETAS), "->")
for sid, slot in allocate_fcfs([(s, opts[s]) for s in ETAS]).items():
    print(f"    {sid}: {slot or 'NO SLOT -> hand-off'}")

print("\n== 3. Stored option set for SHP1006 ==")
ctx, state, r = search("SHP1006")
shown = record_option_set(db, ctx, "THR001", rank_options(r.feasible, 3), state, clock)
print(f"  {shown.option_set_id} shown_at={shown.shown_at:%H:%M:%S} expires_at={shown.expires_at:%H:%M:%S} "
      f"rules={shown.rules_version}")
for it in shown.items:
    d = it.decision
    print(f"   rank {it.rank}: {d.slot_id} {d.slot_start:%H:%M} {d.dock_code} version={it.slot_version} "
          f"hash={it.feasibility_hash}")

print("\n== 4. Driver returns 4 minutes later; planner books option 1 for another truck ==")
taken = shown.items[0].slot_id
db.execute(
    "insert into appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source, "
    "is_current, booked_at, updated_at) values ('APT-DEMO','SHP1018',?, 'CONFIRMED','PLANNER',1, "
    "'2026-08-04T09:38:00+05:30','2026-08-04T09:38:00+05:30')", (taken,))
db.commit()
clock.advance(timedelta(minutes=4))
latest = get_latest_option_set(db, "DRV006", "THR001", "SHP1006")
diff = reconcile_option_set(db, latest, clock)  # type: ignore[arg-type]
print(f"  expired={diff.expired} rules_changed={diff.rules_changed} still_offerable_ranks={diff.still_offerable}")
for i in diff.items:
    print(f"   rank {i.rank} {i.slot_id}: {i.change} {i.reason_codes} -> {i.explanation}")
