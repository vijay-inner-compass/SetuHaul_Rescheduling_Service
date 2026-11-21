"""What happens when a truck reports a new position.

1. The driver is told the position was received: how far the dock is and when they could arrive.
2. A live hold the truck can no longer reach in time is released at once (the dock is freed for others).
3. If the truck is now much closer than its current slot assumed, earlier slots it can reach are offered as a numbered menu
   (nothing is held until the driver picks one; picking one swaps the hold).

Every message is written by the system in the driver's language, from the position and the database, not by a model.
"""

# Lazy annotations.
from __future__ import annotations

# sqlite3 for the Connection type.
import sqlite3

# timedelta adds the drive time to now, and defines the minimum gain.
from datetime import datetime, timedelta

# Ranks feasible slots into a menu, earliest first.
from setuhaul.allocation import rank_options

# Releases a hold the truck can no longer reach.
from setuhaul.booking import reassess_hold_after_position_change

# Facility snapshot for the slot search.
from setuhaul.capacity import build_facility_state

# The injectable clock.
from setuhaul.clock import Clock

# Settings give the GPS freshness, speed and the minimum gain.
from setuhaul.config import load_settings

# Stored times are parsed into aware datetimes.
from setuhaul.db import parse_operational_timestamp

# Feasibility context and slot search (the same rules as any other search).
from setuhaul.feasibility import build_context, search_feasible_slots

# The latest fresh ping.
from setuhaul.gps import load_latest_driver_ping, open_shipments_for_driver

# Driver-facing wording in three languages.
from setuhaul.language import say

# Conversation memory: the driver's thread, language and latest menu.
from setuhaul.memory import ConversationMemoryStore

# Stored menus, so an offer can be held by number and a repeat of the same offer is not sent twice.
from setuhaul.option_history import get_latest_option_set, record_option_set

# The ETA evidence type for a GPS-based arrival.
from setuhaul.schemas import BaselineType, EtaEvidence

# Distance and drive time from the ping to the dock.
from setuhaul.travel import StraightLineEstimator, haversine_km


def _current_slot(conn: sqlite3.Connection, shipment_id: str, now: datetime) -> tuple[datetime, str] | None:
    """The slot the shipment is currently relying on: its live hold, else its current appointment. None if it has neither."""
    # A live hold comes first: that is the choice the driver is in the middle of.
    row = conn.execute(
        "select sl.slot_start_ts, a.slot_id from appointments a join appointment_hold_meta m using(appointment_id) "
        "join appointment_slots sl on sl.slot_id=a.slot_id where a.shipment_id=? and a.appointment_status='HELD' and m.expires_epoch > ?",
        (shipment_id, int(now.timestamp()))).fetchone()
    # Otherwise the current appointment (unloading in progress is excluded: it can no longer move).
    if row is None:
        row = conn.execute(
            "select sl.slot_start_ts, a.slot_id from appointments a join appointment_slots sl on sl.slot_id=a.slot_id "
            "where a.shipment_id=? and a.is_current=1 and a.appointment_status in ('PENDING_CONFIRMATION','CONFIRMED')",
            (shipment_id,)).fetchone()
    # Nothing to improve on.
    if row is None:
        return None
    # Parse the stored start time into an aware datetime.
    return parse_operational_timestamp(row["slot_start_ts"]), str(row["slot_id"])


def handle_new_position(conn: sqlite3.Connection, shipment_id: str, clock: Clock, memory: ConversationMemoryStore,
                        announce: bool = True) -> None:
    """React to a newly stored GPS ping for ``shipment_id``. Best effort: the caller has already stored the ping.

    ``announce`` False skips the "position received" sentence (used for a driver's second and later loads, so one ping is confirmed once)."""
    settings = load_settings()
    now = clock.now()
    # The shipment's driver and the dock (destination facility) it is heading for.
    ship = conn.execute(
        "select s.driver_id, f.lat, f.lon from shipments s join facilities f on f.facility_id = s.destination_facility_id "
        "where s.shipment_id=?", (shipment_id,)).fetchone()
    # Unknown shipment, or a dock without coordinates: nothing can be said.
    if ship is None or ship["lat"] is None or ship["lon"] is None:
        return
    driver_id = ship["driver_id"]
    # The truck's latest fresh position (the ping just stored, which may sit on any of the driver's loads).
    ping = load_latest_driver_ping(conn, driver_id, clock, settings.gps_max_age_minutes)
    if ping is None:
        return
    # Straight-line distance to the dock and the estimated drive, then the earliest arrival.
    km = haversine_km(ping.lat, ping.lon, ship["lat"], ship["lon"])
    minutes = StraightLineEstimator(settings).estimate(ping.lat, ping.lon, ship["lat"], ship["lon"])
    eta = now + timedelta(minutes=minutes)

    # The driver's open conversation, or a new one: the messages below must have somewhere to go.
    row = conn.execute("select thread_id from chat_threads where driver_id=? and thread_status<>'CLOSED' "
                       "order by opened_at desc, rowid desc limit 1", (driver_id,)).fetchone()
    thread_id = row["thread_id"] if row else memory.open_thread(driver_id, shipment_id)
    # Reply in the driver's language (English until one is established).
    lang = memory.load(driver_id, thread_id).state.language or "en"

    # A live hold the truck can no longer reach is released first; the driver is told which one and why.
    released = reassess_hold_after_position_change(conn, shipment_id, clock)
    if released is not None:
        memory.append(driver_id, thread_id, "AGENT", say(
            "position_released", lang, start=released["start"], end=released["end"], dock=released["dock"]))
        return
    # Otherwise confirm the new position: distance, drive time and the earliest arrival (once per ping, for the primary load).
    if announce:
        memory.append(driver_id, thread_id, "AGENT", say(
            "position_received", lang, km=f"{km:.0f}", minutes=str(minutes), eta=f"{eta:%H:%M}"))

    # Earlier slots are only worth offering when the shipment already relies on a slot to improve on.
    current = _current_slot(conn, shipment_id, now)
    if current is None:
        return
    current_start, current_slot_id = current
    # The driver may already have said when they expect to arrive. If that figure (not the truck's position) was the binding limit of
    # the last menu, it still stands: a closer GPS fix alone does not override it (a truck stopped for repair keeps its later figure).
    latest = get_latest_option_set(conn, driver_id, thread_id, shipment_id)
    release, source = eta, "GPS_ASSISTED"
    if latest is not None and latest.context.release_source != "GPS_ASSISTED" and latest.context.release_time > eta:
        release, source = latest.context.release_time, "DRIVER_DECLARED"
    # Search from the earliest the truck can really arrive.
    evidence = EtaEvidence(eta=release, source=source, confidence="MEDIUM", baseline_type=BaselineType.CURRENT_ETA)  # type: ignore[arg-type]
    ctx = build_context(conn, shipment_id, evidence, now)
    state = build_facility_state(conn, ctx.facility_id, now)
    result = search_feasible_slots(ctx, state)
    # Only slots that start clearly earlier than the current one (by the configured gain), and never the held slot itself.
    gain = timedelta(minutes=settings.gps_reoffer_gain_minutes)
    better = [d for d in result.feasible if d.slot_start <= current_start - gain and d.slot_id != current_slot_id]
    ranked = rank_options(better, limit=3)
    if not ranked:
        return
    # Do not send the same offer again for every ping: skip if the latest unexpired menu already starts with this slot.
    if latest is not None and latest.expires_at > now and latest.items and latest.items[0].slot_id == ranked[0].decision.slot_id:
        return
    # Store the menu exactly as it will be shown, and make it the one a number reply refers to.
    shown = record_option_set(conn, ctx, thread_id, ranked, state, clock)
    memory.save_structured_state(driver_id, thread_id, memory.load(driver_id, thread_id).state.model_copy(
        update={"latest_option_set_id": shown.option_set_id}))
    # The numbered list, written from the stored options.
    # Name the order: a driver with two loads must know which one the offer is about.
    order = conn.execute("select order_reference from shipments where shipment_id=?", (shipment_id,)).fetchone()["order_reference"]
    lines = [say("earlier_intro", lang, order=order)]
    for i, o in enumerate(ranked, 1):
        d = o.decision
        lines.append(say("options_line", lang, n=str(i), dock=d.dock_code, start=f"{d.slot_start:%H:%M}",
                         end=f"{d.slot_end:%H:%M}", date=f"{d.slot_start:%d %b %Y}"))
    lines.append(say("options_outro", lang, choices=", ".join(str(i) for i in range(1, len(ranked) + 1)),
                     until=f"{shown.expires_at:%H:%M}"))
    memory.append(driver_id, thread_id, "AGENT", "\n".join(lines))


def handle_driver_position(conn: sqlite3.Connection, driver_id: str, clock: Clock, memory: ConversationMemoryStore) -> None:
    """React to a driver's new position for every one of their open loads. The first load announces the position; each load may
    release its own hold or offer its own earlier slots."""
    for i, shipment_id in enumerate(open_shipments_for_driver(conn, driver_id)):
        handle_new_position(conn, shipment_id, clock, memory, announce=(i == 0))
