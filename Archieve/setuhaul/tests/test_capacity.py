import threading, uuid
from datetime import timedelta
import capacity, database, config
from seed import NOW

def hold(c, sid, slot, key=None, now=NOW):
    return capacity.hold_slot(c, sid, slot, "t", key or uuid.uuid4().hex, now)

def test_integrity_ten_drivers_one_slot(db):
    """Integrity proof: 10 concurrent connections race for ONE slot -> exactly one winner, no invariant violated."""
    results, barrier = [], threading.Barrier(10)
    def worker(i):
        conn = database.connect(db); barrier.wait()
        results.append(capacity.hold_slot(conn, f"SHP{1000+i}", "D1-12", f"t{i}", f"key-{i:04d}-{uuid.uuid4().hex[:6]}", NOW))
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(1, 11)]
    [t.start() for t in ts]; [t.join() for t in ts]
    winners = [r for r in results if r["ok"]]
    assert len(results) == 10 and len(winners) == 1
    assert all(r["code"] == "taken" for r in results if not r["ok"])
    assert capacity.check_invariants(database.connect(db)) == []

def test_showing_is_not_reserving(c):
    assert c.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 0
    opts = capacity.find_options(c, "SHP1001", NOW)
    assert opts and c.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 0

def test_stale_option_refused_with_reason(c):
    opts = capacity.find_options(c, "SHP1001", NOW)
    first = opts[0]["slot_id"]
    assert hold(c, "SHP1002", first)["ok"]                      # another driver takes it after SHP1001 saw it
    r = hold(c, "SHP1001", first)
    assert not r["ok"] and r["code"] == "taken" and r["why"]

def test_hold_expiry_reopens_slot_and_blocks_confirm(c):
    h = hold(c, "SHP1001", "D1-12")
    later = NOW + config.HOLD_TTL + timedelta(minutes=1)
    assert "D1-12" in [o["slot_id"] for o in capacity.find_options(c, "SHP1002", later, limit=50)]
    r = capacity.confirm(c, h["hold_id"], later)
    assert not r["ok"] and r["code"] == "hold_expired"
    assert hold(c, "SHP1002", "D1-12", now=later)["ok"]

def test_idempotent_duplicate_message(c):
    a = hold(c, "SHP1001", "D1-12", key="same-key-1"); b = hold(c, "SHP1001", "D1-12", key="same-key-1")
    assert a == b and c.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 1

def test_incompatible_and_blocked_and_before_eta(c):
    c.execute("UPDATE shipments SET vehicle_type='reefer' WHERE shipment_id='SHP1001'")
    assert hold(c, "SHP1001", "D1-12")["code"] == "incompatible_dock"
    assert hold(c, "SHP1002", "D1-10")["code"] == "blocked"
    assert hold(c, "SHP1002", "D1-09")["code"] == "before_eta"          # 09:00 start < ETA 09:00 + 15 min
    assert hold(c, "SHP1001", "D3-12")["ok"]

def test_confirm_swaps_old_booking_atomically(c):
    h1 = hold(c, "SHP1001", "D1-12"); assert capacity.confirm(c, h1["hold_id"], NOW)["ok"]
    h2 = hold(c, "SHP1001", "D1-14"); assert capacity.confirm(c, h2["hold_id"], NOW)["ok"]
    st = {r["slot_id"]: r["status"] for r in c.execute("SELECT slot_id,status FROM appointments")}
    assert st == {"D1-12": "released", "D1-14": "confirmed"} and capacity.check_invariants(c) == []

def test_priority_policy_and_no_preemption(c):
    c.execute("UPDATE shipments SET priority='high' WHERE shipment_id='SHP1005'")
    reqs = [dict(shipment_id=f"SHP{1000+i}", thread_id="t", key=f"batch-key-{i:03d}") for i in (1, 2, 5)]
    out = dict(capacity.allocate_batch(c, reqs, NOW))
    slots = {s: r["slot_id"] for s, r in out.items()}
    assert slots["SHP1005"] == "D1-09" or slots["SHP1005"] < slots["SHP1001"]    # high priority picks first
    assert len(set(slots.values())) == 3 and capacity.check_invariants(c) == []
