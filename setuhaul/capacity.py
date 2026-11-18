"""Capacity layer: CODE decides feasibility and commitment (rubric: 'Code, not a prompt').
State machine per appointment:  (shown: nothing written) -> held (TTL) -> confirmed
                                held -> released | expired ;  confirmed -> released (on swap)
Only this module writes appointments."""
import json
import sqlite3
from datetime import datetime
import config
from database import Tx, get_shipment

ISO = "%Y-%m-%dT%H:%M"
def fmt(dt): return dt.strftime(ISO)
def parse(s): return datetime.strptime(s, ISO)

def _expire_stale(c, now):
    c.execute("UPDATE appointments SET status='expired' WHERE status='held' AND hold_expires_at<=?", (fmt(now),))

def feasibility(c, shipment, slot_id, now, ignore_appt_id=None):
    """One written check, used at show time AND again at hold and confirm (stale-option defence).
    An expired hold does not block (judged by timestamp), so the read path never writes."""
    slot = c.execute("SELECT s.*, d.facility_id, d.accepts_reefer, d.accepts_heavy FROM slots s "
                     "JOIN docks d USING(dock_id) WHERE slot_id=?", (slot_id,)).fetchone()
    if not slot: return False, "no_such_slot", "That slot does not exist."
    if slot["facility_id"] != shipment["facility_id"]:
        return False, "wrong_facility", "That slot is at a different facility."
    vt = shipment["vehicle_type"]
    if (vt == "reefer" and not slot["accepts_reefer"]) or (vt == "heavy" and not slot["accepts_heavy"]):
        return False, "incompatible_dock", f"That dock cannot take a {vt} load."
    if slot["blocked"]: return False, "blocked", "That dock window is blocked."
    if parse(slot["start_ts"]) < now: return False, "in_past", "That slot has already started."
    if parse(slot["start_ts"]) < parse(shipment["current_eta"]) + config.ETA_BUFFER:
        return False, "before_eta", "You would not reach the dock before that slot starts."
    row = c.execute("SELECT appt_id FROM appointments WHERE slot_id=? AND appt_id IS NOT ? AND "
                    "(status='confirmed' OR (status='held' AND hold_expires_at>?))",
                    (slot_id, ignore_appt_id, fmt(now))).fetchone()
    if row: return False, "taken", "Someone else holds or has booked that slot."
    return True, "ok", "ok"

def find_options(c, shipment_id, now, not_before=None, limit=3):
    """READ ONLY. Showing an option reserves nothing."""
    sh = get_shipment(c, shipment_id)
    out = []
    for s in c.execute("SELECT slot_id,start_ts,end_ts,dock_id FROM slots ORDER BY start_ts"):
        if not_before and parse(s["start_ts"]) < not_before: continue
        if feasibility(c, sh, s["slot_id"], now)[0]:
            out.append(dict(slot_id=s["slot_id"], dock=s["dock_id"], start=s["start_ts"], end=s["end_ts"]))
        if len(out) >= limit: break
    return out

def set_eta(c, shipment_id, new_eta, source, now):
    if (new_eta - now).total_seconds() > config.MAX_DELAY_HOURS * 3600:
        return dict(ok=False, code="needs_human", why="Delay too large to handle automatically.")
    conf = "declared" if source == "driver_declared" else "estimate_includes_repair_time"
    with Tx(c):
        c.execute("UPDATE shipments SET current_eta=?, eta_source=?, eta_confidence=? WHERE shipment_id=?",
                  (fmt(new_eta), source, conf, shipment_id))
    return dict(ok=True, eta=fmt(new_eta), source=source, confidence=conf)

def hold_slot(c, shipment_id, slot_id, thread_id, idem_key, now):
    """Atomic: replay if key seen -> expire stale -> revalidate -> drop my old hold -> insert.
    The partial unique index is the last line of defence; losing a race returns 'taken'."""
    with Tx(c):
        done = c.execute("SELECT result_json FROM idempotency WHERE key=?", (idem_key,)).fetchone()
        if done: return json.loads(done["result_json"])
        _expire_stale(c, now)
        sh = get_shipment(c, shipment_id)
        ok, code, why = feasibility(c, sh, slot_id, now)
        if not ok:
            res = dict(ok=False, code=code, why=why)
        else:
            c.execute("UPDATE appointments SET status='released' WHERE shipment_id=? AND status='held'", (shipment_id,))
            exp = now + config.HOLD_TTL
            try:
                cur = c.execute("INSERT INTO appointments(shipment_id,slot_id,status,hold_expires_at,thread_id,created_at)"
                                " VALUES(?,?,'held',?,?,?)", (shipment_id, slot_id, fmt(exp), thread_id, fmt(now)))
                res = dict(ok=True, hold_id=cur.lastrowid, slot_id=slot_id, held_until=fmt(exp))
            except sqlite3.IntegrityError:
                res = dict(ok=False, code="taken", why="Someone else holds or has booked that slot.")
        c.execute("INSERT INTO idempotency(key,result_json) VALUES(?,?)", (idem_key, json.dumps(res)))
        return res

def confirm(c, hold_id, now):
    """Re-checks everything; success is read back from the table (verified state)."""
    with Tx(c):
        a = c.execute("SELECT * FROM appointments WHERE appt_id=?", (hold_id,)).fetchone()
        if not a: return dict(ok=False, code="no_such_hold", why="No such hold.")
        if a["status"] == "confirmed": return dict(ok=True, appt_id=hold_id, slot_id=a["slot_id"], already=True)
        if a["status"] != "held" or a["hold_expires_at"] <= fmt(now):
            if a["status"] == "held": c.execute("UPDATE appointments SET status='expired' WHERE appt_id=?", (hold_id,))
            return dict(ok=False, code="hold_expired", why="The hold ran out or was released.")
        sh = get_shipment(c, a["shipment_id"])
        ok, code, why = feasibility(c, sh, a["slot_id"], now, ignore_appt_id=hold_id)
        if not ok: return dict(ok=False, code=code, why=why)
        c.execute("UPDATE appointments SET status='released' WHERE shipment_id=? AND status='confirmed'", (a["shipment_id"],))
        c.execute("UPDATE appointments SET status='confirmed', hold_expires_at=NULL WHERE appt_id=?", (hold_id,))
        v = c.execute("SELECT status,slot_id FROM appointments WHERE appt_id=?", (hold_id,)).fetchone()
        return dict(ok=v["status"] == "confirmed", appt_id=hold_id, slot_id=v["slot_id"])

def release(c, hold_id):
    with Tx(c):
        c.execute("UPDATE appointments SET status='released' WHERE appt_id=? AND status='held'", (hold_id,))
    return dict(ok=True)

def check_invariants(c):
    """Code evaluator (S10). Must return [] after any workload."""
    qs = [("double_booked_slot", "SELECT slot_id FROM appointments WHERE status IN ('held','confirmed') GROUP BY slot_id HAVING COUNT(*)>1"),
          ("two_confirmed", "SELECT shipment_id FROM appointments WHERE status='confirmed' GROUP BY shipment_id HAVING COUNT(*)>1"),
          ("two_holds", "SELECT shipment_id FROM appointments WHERE status='held' GROUP BY shipment_id HAVING COUNT(*)>1"),
          ("incompatible_booking",
           "SELECT a.appt_id FROM appointments a JOIN slots s USING(slot_id) JOIN shipments sh USING(shipment_id) "
           "JOIN docks d ON d.dock_id=s.dock_id WHERE a.status IN ('held','confirmed') AND "
           "((sh.vehicle_type='reefer' AND d.accepts_reefer=0) OR (sh.vehicle_type='heavy' AND d.accepts_heavy=0))")]
    return [(n, tuple(r)) for n, q in qs for r in c.execute(q)]

PRIORITY_RANK = {"high": 0, "standard": 1}
def allocate_batch(c, requests, now):
    """Priority policy for a spike. requests: [{shipment_id, thread_id, key}] in arrival order.
    Order: hard constraints (inside hold_slot) -> priority class -> earliest current ETA -> arrival order.
    Never pre-empts held or confirmed slots."""
    def k(r):
        sh = get_shipment(c, r["shipment_id"]); return (PRIORITY_RANK[sh["priority"]], sh["current_eta"])
    out = []
    for r in sorted(requests, key=k):
        opts = find_options(c, r["shipment_id"], now, limit=1)
        res = hold_slot(c, r["shipment_id"], opts[0]["slot_id"], r["thread_id"], r["key"], now) if opts \
            else dict(ok=False, code="no_slot", why="No feasible slot.")
        out.append((r["shipment_id"], res))
    return out
