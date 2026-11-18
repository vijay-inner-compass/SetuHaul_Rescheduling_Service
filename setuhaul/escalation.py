"""Hand-off record: a human gets everything needed to act without re-asking the driver (decisions 10-12)."""
import json
from capacity import fmt

def escalate(c, driver_id, thread_id, shipment_id, reason, now, note=""):
    sh = c.execute("SELECT * FROM shipments WHERE shipment_id=?", (shipment_id,)).fetchone() if shipment_id else None
    appts = [dict(r) for r in c.execute("SELECT appt_id,slot_id,status,hold_expires_at FROM appointments WHERE shipment_id=?", (shipment_id,))] if sh else []
    hist = [dict(r) for r in c.execute("SELECT role,content FROM messages WHERE driver_id=? AND thread_id=? ORDER BY message_id DESC LIMIT 8", (driver_id, thread_id))]
    packet = dict(driver_id=driver_id, shipment=dict(sh) if sh else None, appointments=appts,
                  recent_messages=list(reversed(hist)), reason=reason, note=note,
                  suggested_next_step="Call the driver; offer manual dock placement or reschedule the consignee.")
    cur = c.execute("INSERT INTO escalations(thread_id,shipment_id,reason,packet_json,created_at) VALUES(?,?,?,?,?)",
                    (thread_id, shipment_id, reason, json.dumps(packet), fmt(now)))
    return dict(ok=True, escalation_id=cur.lastrowid)
