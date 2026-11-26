"""The Shipments tab of the Warehouse screen: open availability, create a shipment, follow the assigned ones."""

# Lazy annotations.
from __future__ import annotations

import sqlite3
from datetime import datetime

from setuhaul import passes
from setuhaul.auth.authz import facility_clause
from setuhaul.auth.onboarding import driver_eligible
from setuhaul.auth.pages import _button, _segment, _table, esc
from setuhaul.auth.shipments import list_shipments
from setuhaul.auth.users import User
from setuhaul.auth.warehouse_routes import drivers_in_scope
from setuhaul.db import IST, parse_operational_timestamp

_PASS = {"ISSUED": "Sent", "VALIDATED": "At the gate", "ACTIVE": "Inside", "COMPLETED": "Unloaded", "CLOSED": "Closed",
         "EXPIRED": "Expired", "REVOKED": "Revoked", "CANCELLED": "Cancelled"}
_STATES = {"WAITING": "Waiting for driver", "CONFIRMED": "Confirmed", "LOCKED": "Locked", "CANCELLED": "Cancelled"}


def _option(value: str, label: str, **data: str) -> str:
    extra = "".join(f' data-{key}="{esc(v)}"' for key, v in data.items())
    return f'<option value="{esc(value)}"{extra}>{esc(label)}</option>'


def _select(label: str, name: str, options: str, extra: str = "required") -> str:
    return f'<label>{esc(label)} <select name="{name}" {extra}>{options}</select></label>'


def _input(label: str, name: str, kind: str = "text", extra: str = "required") -> str:
    return f'<label>{esc(label)} <input name="{name}" type="{kind}" {extra}></label>'


def _yes_no(label: str, name: str, yes_first: bool = False) -> str:
    options = [_option("false", "No"), _option("true", "Yes")]
    return _select(label, name, "".join(reversed(options) if yes_first else options))


def _when(stamp: str) -> str:
    return f"{parse_operational_timestamp(stamp):%d %b %Y %H:%M}"


def shipments_section(conn: sqlite3.Connection, user: User, now: datetime, facilities: list[tuple[str, str]]) -> str:
    clause, params = facility_clause(user, "f.facility_id")
    own = [r["facility_id"] for r in conn.execute(
        f"select f.facility_id from facilities f where {clause} and f.active_flag=1 and f.is_partner=0", params)]
    places = "".join(_option(v, label) for v, label in facilities if v in own)
    marks = ",".join("?" * len(own)) or "null"
    docks = "".join(_option(r["dock_id"], f"{r['dock_id']} ({r['dock_type']})", facility=r["facility_id"]) for r in conn.execute(
        f"select dock_id, facility_id, dock_type from docks where facility_id in ({marks}) order by dock_id", own))
    # Only slots a new shipment could take: ahead of now, open and not booked or held. Asked per facility, so a busy one
    # cannot crowd another off the list.
    slots = ""
    for facility_id in own:
        free = conn.execute(
            "select s.slot_id, d.dock_code, s.slot_start_ts from appointment_slots s join docks d using(dock_id)"
            " where s.facility_id=? and s.slot_status='OPEN' and s.slot_start_ts>? and not exists (select 1 from appointments a"
            " where a.slot_id=s.slot_id and a.appointment_status in ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS','HELD'))"
            " order by s.slot_start_ts, d.dock_code limit 3000", (facility_id, now.astimezone(IST).isoformat())).fetchall()
        slots += "".join(_option(r["slot_id"], f"{facility_id} {r['dock_code']} {_when(r['slot_start_ts'])}",
                                 facility=facility_id) for r in free)
        if not free:
            slots += _option("", "No open slots: open availability first", facility=facility_id)
    today = now.astimezone(IST).date()
    able = [r for r in drivers_in_scope(conn, user, today) if driver_eligible(conn, r["driver_id"], today)]
    drivers = "".join(_option(r["driver_id"], f"{r['driver_id']} - {r['driver_name']}", carrier=r["carrier_id"]) for r in able)
    vehicles = "".join(_option(r["vehicle_id"], f"{r['vehicle_id']} - {r['registration_number']}", carrier=r["carrier_id"])
                       for r in conn.execute("select vehicle_id, carrier_id, registration_number from vehicles where active_flag=1 "
                                             "order by vehicle_id"))
    availability = (
        '<h3>1. Open availability</h3><p class="note">Adds the dock slots and labour that bookings need. Existing ones are kept.</p>'
        '<form id="availability-form" data-limit>'
        + _select("Facility", "facility_id", places) + _input("First date", "first_date", "date")
        + _input("Last date (at most 14 days)", "last_date", "date")
        + _select("Docks", "dock_ids", docks, "multiple required") + _input("From", "from_time", "time")
        + _input("To", "to_time", "time") + _input("Slot minutes", "slot_minutes", "number", 'value="60" min="15" max="480" required')
        + _input("Crews", "crews", "number", 'value="4" min="1" max="100" required')
        + _yes_no("Equipment available", "equipment_available", yes_first=True)
        + '<button type="submit">Open availability</button></form><p class="note" id="availability-result" role="status"></p>')
    new = (
        '<h3>2. New shipment</h3><p class="note">The bot sends the driver a Dock Access Pass, which they send back to confirm.</p>'
        '<form data-api="/v1/warehouse/shipments" data-limit>'
        + _input("Order number", "order_number", extra='required maxlength="40" placeholder="ORD then at least 6 digits"')
        + _select("Driver", "driver_id", drivers) + _select("Vehicle", "vehicle_id", vehicles)
        + _input("Origin name", "origin_name") + _input("Origin city", "origin_city")
        + _select("Destination facility", "facility_id", places) + _input("Customer", "customer_name")
        + _input("Product", "product_category") + _input("Load (kg)", "load_weight_kg", "number", 'min="1" required')
        + _input("Pallets", "pallet_count", "number", 'min="0"')
        + _select("Dock type", "required_dock_type", "".join(_option(v, v) for v in ("STANDARD", "REEFER", "HEAVY", "ANY")))
        + _yes_no("Refrigerated", "temperature_control_required")
        + _select("Priority", "priority_code", "".join(_option(v, v) for v in ("NORMAL", "LOW", "HIGH", "CRITICAL")))
        + _input("Unload minutes", "expected_unload_min", "number", 'value="50" min="1" required')
        + _input("Planned departure", "planned_departure", "datetime-local")
        + _select("Planned dock slot", "slot_id", slots)
        + _input("Latest arrival (optional)", "latest_arrival", "datetime-local", "")
        + _yes_no("Penalty if later", "penalty_applies")
        + '<button type="submit">Create shipment</button><p class="note" data-form-result role="alert"></p></form>')
    rows = []
    for s in list_shipments(conn, user):
        base = f"/v1/warehouse/shipments/{_segment(s['shipment_id'])}"
        actions = ""
        if s["state"] in ("WAITING", "LOCKED"):
            actions = (f'<form data-api="{esc(base)}/driver" data-limit>' + _select("Driver", "driver_id", drivers)
                       + _select("Vehicle", "vehicle_id", vehicles) + '<button type="submit">Change driver</button><span class="note" data-form-result role="alert"></span></form>'
                       + _button("Cancel", f"{base}/cancel", confirm=f"Cancel {s['order_reference']}?"))
        if s["state"] == "LOCKED":
            actions += _button("Unlock", f"{base}/unlock")
        if s["state"] != "CANCELLED" and s["visit_state"] in (None, "EXPECTED"):
            actions += _button("Send new pass", f"{base}/pass", confirm=f"Replace the pass for {s['order_reference']}?")
        if s["pass_state"] in ("ISSUED", "VALIDATED", "ACTIVE", "COMPLETED"):
            actions += (f'<form data-api="{esc(base)}/pass/revoke">' + _input("Reason", "reason", extra='required maxlength="200"')
                        + '<button type="submit">Revoke pass</button><span class="note" data-form-result role="alert"></span></form>')
        visit = passes.visit_for(conn, s["shipment_id"])
        if visit is not None and visit["status"] == "CHECKED_IN":
            actions += _button("Unloading complete", f"/v1/warehouse/visits/{_segment(visit['truck_visit_id'])}/unloading-complete",
                               confirm=f"Has {s['order_reference']} been unloaded?")
        pass_text = _PASS.get(s["pass_state"] or "", "None")
        if s["pass_state"] == "ISSUED" and not s["delivered"]:
            pass_text = "Pass not delivered"
        rows.append([esc(s["order_reference"]), esc(f"{s['driver_id']} - {s['driver_name']}"), esc(s["facility_id"]),
                     esc(_when(s["planned_start"])), esc(_STATES[s["state"]]), esc(pass_text), actions])
    table = _table(("Order", "Driver", "Destination", "Planned", "State", "Pass", ""), rows, "No shipments have been created here yet.")
    return (f'<section id="shipments" data-panel="shipments"><h2>Shipments</h2><p class="note">This tab does not update by itself. Reload the page to see changes.</p>{table}'
            f"{availability}{new}</section>")
