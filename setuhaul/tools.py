"""Tools: describe + call (S1, S5). Built per request from the token; NO tool takes driver_id (S2 lock 1).
Each tool validates with its Pydantic args_schema first (S6), then checks ownership (S7), then calls capacity (code decides)."""
import json
from datetime import timedelta
from pydantic import ValidationError
import capacity, escalation
from database import get_owned_shipment
import models as M

def make_tools(c, driver, thread_id, now, tracer):
    did = driver["driver_id"]

    def owned(sid):
        sh = get_owned_shipment(c, did, sid)
        if not sh: raise LookupError("No such shipment for this driver.")   # same answer for 'foreign' and 'missing'
        return sh

    def get_my_shipments(a):
        rows = c.execute("SELECT s.shipment_id,s.vehicle_type,s.facility_id,s.current_eta,s.eta_source,s.eta_confidence,"
                         "a.appt_id AS appointment_id,a.slot_id AS appointment_slot,a.status AS appointment_status "
                         "FROM shipments s LEFT JOIN appointments a ON a.shipment_id=s.shipment_id AND a.status IN ('held','confirmed') "
                         "WHERE s.driver_id=? ORDER BY s.shipment_id, a.status", (did,)).fetchall()
        return dict(ok=True, shipments=[dict(r) for r in rows])

    def report_delay(a):
        sh = owned(a.shipment_id)
        if a.delay_minutes is not None:
            new = max(now, capacity.parse(sh["current_eta"])) + timedelta(minutes=a.delay_minutes)
        else:
            new = a.new_eta
        return capacity.set_eta(c, a.shipment_id, new, a.source, now)

    def find_options(a):
        owned(a.shipment_id)
        return dict(ok=True, note="These are NOT reserved.", options=capacity.find_options(c, a.shipment_id, now, a.not_before, a.limit))

    def hold_option(a):
        owned(a.shipment_id)
        return capacity.hold_slot(c, a.shipment_id, a.slot_id, thread_id, a.idempotency_key, now)

    def _own_hold(hold_id):
        r = c.execute("SELECT s.driver_id FROM appointments a JOIN shipments s USING(shipment_id) WHERE a.appt_id=?", (hold_id,)).fetchone()
        if not r or r["driver_id"] != did: raise LookupError("No such hold for this driver.")

    def confirm_booking(a): _own_hold(a.hold_id); return capacity.confirm(c, a.hold_id, now)
    def release_hold(a): _own_hold(a.hold_id); return capacity.release(c, a.hold_id)
    def escalate_to_human(a):
        owned(a.shipment_id)
        return escalation.escalate(c, did, thread_id, a.shipment_id, a.reason, now)

    reg = {"get_my_shipments": (M.NoArgs, get_my_shipments, "List this driver's open shipments, ETA and current appointment. Read only. Call first when unsure which load."),
           "report_delay": (M.ReportDelay, report_delay, "Record a revised ETA from a delay in minutes OR a clock time. source=driver_estimate_with_repair if the figure includes repair time."),
           "find_options": (M.FindOptions, find_options, "Read-only list of feasible later slots. Showing an option reserves nothing."),
           "hold_option": (M.HoldOption, hold_option, "Hold ONE slot for 10 minutes after the driver says yes. Reuse the same idempotency_key on retries."),
           "confirm_booking": (M.ConfirmBooking, confirm_booking, "Confirm a hold after the driver confirms. Only this makes a booking."),
           "release_hold": (M.ReleaseHold, release_hold, "Drop a hold the driver no longer wants."),
           "escalate_to_human": (M.Escalate, escalate_to_human, "Hand to a coordinator when no safe automatic outcome exists.")}

    def specs():
        return [dict(name=n, description=d, input_schema=s.model_json_schema()) for n, (s, _, d) in reg.items()]

    def call(name, args):
        """Returns a dict. Errors go BACK TO THE MODEL as data (S1: wrong ID reaches the model), never raised."""
        if name not in reg: return dict(ok=False, code="unknown_tool", why=f"No tool named {name}.")
        schema, fn, _ = reg[name]
        with tracer.span(f"tool.{name}"):
            try: return fn(schema(**(args or {})))
            except ValidationError as e: return dict(ok=False, code="invalid_arguments", why=str(e.errors()[0]["msg"]))
            except LookupError as e: return dict(ok=False, code="not_found", why=str(e))
    return specs, call
