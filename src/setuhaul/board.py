"""The dock-by-hour board for one facility, shared by the demo console and the warehouse API."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from setuhaul.capacity import build_facility_state
from setuhaul.db import IST
from setuhaul.release import booking, not_freeable


def build_board(conn: sqlite3.Connection, facility_id: str, now: datetime) -> dict[str, Any] | None:
    # Unknown facility: nothing to draw.
    facility = conn.execute("select is_partner from facilities where facility_id=?", (facility_id,)).fetchone()
    if facility is None:
        return None
    # The same snapshot the service uses to judge slots.
    state = build_facility_state(conn, facility_id, now)
    # Who occupies each slot, as 'driver . order'.
    owner = {r["slot_id"]: (r["driver_id"], f"{r['driver_name']} · {r['order_reference']}", r["booking_source"]) for r in conn.execute(
        "select a.slot_id, a.booking_source, d.driver_id, d.driver_name, s.order_reference from appointments a join shipments s using(shipment_id) "
        "join drivers d on d.driver_id=s.driver_id where a.appointment_status in ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS','HELD')")}
    # One entry per dock.
    docks: list[dict[str, Any]] = []
    # Docks in code order (D1, D2 ...).
    for dock in sorted(state.docks.values(), key=lambda d: d.code):
        # Slots of this dock.
        slots: list[dict[str, Any]] = []
        # Slots in time order.
        for slot in sorted((s for s in state.slots.values() if s.dock_id == dock.dock_id), key=lambda s: s.start):
            # Who occupies it, if anyone (unexpired holds count).
            occ = state.occupancy.get(slot.slot_id)
            # Blocked by a planner, an out-of-service dock or a maintenance event.
            events = [e for e in state.effective_events(dock.dock_id) if e.start < slot.end and (e.end is None or e.end > slot.start)]
            blocked = slot.status != "OPEN" or dock.status != "ACTIVE" or bool(events)
            # BLOCKED, HELD, BOOKED or AVAILABLE.
            status = "BLOCKED" if blocked else ("HELD" if occ and occ.status == "HELD" else "BOOKED" if occ else "AVAILABLE")
            # The warehouse may free a booking that has not started; partner bookings are agreed by email.
            found = booking(conn, occ.appointment_id) if occ and not facility["is_partner"] and slot.start > now else None
            freeable = found is not None and not_freeable(conn, found) is None
            # The occupant's label.
            who = owner.get(slot.slot_id) if occ else None
            # One cell of the board.
            slots.append({"slot_id": slot.slot_id, "hour": slot.start.astimezone(IST).hour, "start": f"{slot.start.astimezone(IST):%H:%M}",
                          "end": f"{slot.end.astimezone(IST):%H:%M}", "status": status, "past": slot.start < now,
                          # Full times: partner bookings can start partway through an hour.
                          "start_ts": slot.start.astimezone(IST).isoformat(), "end_ts": slot.end.astimezone(IST).isoformat(),
                          "label": who[1] if who else (slot.block_reason or (events[0].reason if events else "")), "driver_id": who[0] if who else None,
                          "source": who[2] if who else None, "appointment_id": occ.appointment_id if occ else None,
                          "freeable": freeable,
                          # Kept apart from the label: a blocked slot may also have a booking.
                          "reason": (slot.block_reason or (events[0].reason if events else "")) if status == "BLOCKED" else "", "partner": bool(who and who[2] == "WAREHOUSE")})
        # One row of the board.
        docks.append({"code": dock.code, "type": dock.dock_type.title(), "max_kg": dock.max_weight_kg, "slots": slots})
    # The hours that have slots, for the column headings.
    hours = sorted({s["hour"] for d in docks for s in d["slots"]})
    # Requests still waiting for a partner's reply: shown beside the board, never as booked slots.
    pending = [{"request_ref": r["request_ref"], "order_reference": r["order_reference"], "driver": r["driver_name"],
                "start": f"{datetime.fromisoformat(r['proposed_start']).astimezone(IST):%H:%M}",
                "end": f"{datetime.fromisoformat(r['proposed_end']).astimezone(IST):%H:%M}",
                "expires_at": r["expires_at"]} for r in conn.execute(
        "select r.request_ref, r.proposed_start, r.proposed_end, r.expires_at, s.order_reference, d.driver_name "
        "from partner_requests r join shipments s using(shipment_id) join drivers d on d.driver_id=s.driver_id "
        "where r.facility_id=? and r.status='PENDING_PARTNER' order by r.created_at", (facility_id,))]
    return {"facility": facility_id, "hours": hours, "docks": docks, "pending_partner": pending}
