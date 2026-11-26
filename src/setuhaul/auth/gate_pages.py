"""The gate screen: check a pass, check the truck in, and check out the trucks inside."""

# Lazy annotations.
from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime

from fastapi.responses import HTMLResponse

from setuhaul import gate
from setuhaul.auth.pages import _button, _layout, _respond, _table, esc
from setuhaul.auth.users import User
from setuhaul.clock import Clock
from setuhaul.db import IST


def render_gate(conn: sqlite3.Connection, user: User, clock: Clock) -> HTMLResponse:
    check = (
        '<section><h2>Arriving truck</h2><p class="note">Type the vehicle number you see on the truck and the number on the '
        "driver's licence. Compare the licence photo with the driver yourself.</p>"
        '<form id="gate-form">'
        '<label>Dock Access Pass <input name="code" required maxlength="80" autocomplete="off" spellcheck="false"></label>'
        '<label>Vehicle number <input name="vehicle_registration" required maxlength="40" autocomplete="off"></label>'
        '<label>Licence number <input name="licence_number" required maxlength="60" autocomplete="off"></label>'
        '<button type="submit">Check pass</button></form>'
        '<p class="note" id="gate-result" role="alert"></p><button type="button" id="check-in" hidden>Check in</button></section>')
    rows = []
    for v in gate.inside(conn, user, clock):
        state = "Unloaded, may leave" if v["status"] == "OPERATIONS_COMPLETE" else "Unloading"
        if v["overstayed"]:
            state += " (past pass window)"
        action = _button("Check out", "/v1/gate/check-out",
                         body={"truck_visit_id": v["truck_visit_id"], "request_key": "out-" + v["truck_visit_id"]},
                         confirm=f"Check out {v['vehicle_registration']}?")
        since = datetime.fromisoformat(v["checked_in_at"]).astimezone(IST)
        rows.append([esc(v["vehicle_registration"]), esc(v["driver_name"]), esc(v["order_reference"]),
                     esc(f"{since:%d %b %H:%M}"), esc(state), action])
    table = _table(("Vehicle", "Driver", "Order", "Inside since", "State", ""), rows, "No trucks are inside.")
    leave = (
        '<form data-api="/v1/gate/check-out"><label>Or check out by pass <input name="code" required maxlength="80" '
        f'autocomplete="off" spellcheck="false"></label><input type="hidden" name="request_key" value="{secrets.token_hex(8)}">'
        '<button type="submit">Check out by pass</button><p class="note" data-form-result role="alert"></p></form>')
    body = f'<div class="wh">{check}<section><h2>Trucks inside</h2><div class="scroll">{table}</div>{leave}</section></div>'
    nav = '<a href="/gate">Gate</a>' + (' <a href="/warehouse">Warehouse</a> <a href="/admin">Admin</a>' if user.role == "ADMIN" else "")
    return _respond(_layout("Gate", body, ("common.js", "gate.js"), user, nav))
