"""Screen parts for warehouse exceptions: blocking a dock, the blocks in force, and deleted drivers."""

# Lazy annotations.
from __future__ import annotations

import sqlite3
from datetime import datetime

from setuhaul import dock_blocks
from setuhaul.auth import driver_removal
from setuhaul.auth.authz import facility_ids
from setuhaul.auth.pages import _button, _options, _segment, _table, esc
from setuhaul.auth.users import User
from setuhaul.db import IST, parse_operational_timestamp

_TYPES = [("MAINTENANCE", "Maintenance"), ("BREAKDOWN", "Breakdown"), ("MANUAL_BLOCK", "Other block")]


def _when(stamp: str) -> str:
    return f"{parse_operational_timestamp(stamp).astimezone(IST):%d %b %H:%M}"


def blocks_section(conn: sqlite3.Connection, user: User, facility: str, now: datetime) -> str:
    scope = facility_ids(user)
    own = conn.execute("select 1 from facilities where facility_id=? and active_flag=1 and is_partner=0", (facility,)).fetchone()
    if own is None or (scope is not None and facility not in scope):
        return ""
    docks = [(r["dock_id"], f"{r['dock_code']} ({r['dock_type'].title()})") for r in conn.execute(
        "select dock_id, dock_code, dock_type from docks where facility_id=? order by dock_code", (facility,))]
    rows = []
    for b in dock_blocks.current(conn, [facility], now):
        until = _when(b["ends"]) if b["ends"] else "until reopened"
        reopen = _button("Reopen", f"/v1/warehouse/dock-blocks/{_segment(b['dock_event_id'])}/reopen",
                         confirm=f"Reopen dock {b['dock_code']}? Bookings already moved stay where they are.")
        rows.append([esc(b["dock_code"]), esc(_when(b["starts"])), esc(until), esc(b["reason"]), reopen])
    form = ('<form id="block-form" class="filters">'
            f'<input type="hidden" name="facility_id" value="{esc(facility)}">'
            f'<label>Dock <select name="dock_id">{_options(docks)}</select></label>'
            f'<label>Type <select name="event_type">{_options(_TYPES)}</select></label>'
            '<label>From (India time) <input name="starts" type="datetime-local" required></label>'
            '<label>To (empty: until reopened) <input name="ends" type="datetime-local"></label>'
            '<label>Reason <input name="reason" maxlength="200" required></label>'
            '<button type="submit">Check bookings</button>'
            '<div id="block-warning"></div></form>')
    table = _table(("Dock", "From", "To", "Reason", ""), rows, "No dock is blocked.")
    return (f'<h3>Block a dock</h3>{form}'
            f'<h3>Current and upcoming blocks</h3><div class="blocks-list">{table}</div>')


def deleted_section(conn: sqlite3.Connection) -> str:
    """The admin tab listing deleted drivers, each with Restore."""
    rows = []
    for d in driver_removal.deleted(conn):
        restore = _button("Restore", f"/v1/drivers/{_segment(d['driver_id'])}/restore",
                          confirm=f"Restore {d['driver_name']}? Their cancelled shipments do not come back.")
        rows.append([esc(d["driver_id"]), esc(d["driver_name"]), esc(d["carrier_id"]), esc(_when(d["deleted_at"])),
                     esc(d["deleted_by"]), restore])
    table = _table(("Driver", "Name", "Carrier", "Deleted", "By", ""), rows, "No drivers are deleted.")
    return ('<section id="deleted" data-panel="deleted"><h2>Deleted drivers</h2><p class="note">A restored driver must be allowed '
            f'to re-link Telegram and be allocated to projects again.</p><div class="scroll">{table}</div></section>')
