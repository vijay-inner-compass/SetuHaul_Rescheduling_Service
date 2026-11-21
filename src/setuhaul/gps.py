"""GPS ping storage and lookup.

Pings are stored with every timestamp normalised to IST so text order in the index is chronological order.
"""

# Lazy annotations.
from __future__ import annotations

# sqlite3 for the Connection type.
import sqlite3

# datetime types the arrival floor; timedelta expresses the freshness window and travel time.
from datetime import datetime, timedelta

# The injectable clock supplies "now".
from setuhaul.clock import Clock

# Settings carries the freshness limit and travel-time values.
from setuhaul.config import Settings

# IST normalises stored times; the parser reads them back.
from setuhaul.db import IST, parse_operational_timestamp

# The ping shape this module stores and returns.
from setuhaul.schemas import GpsPing

# The estimator turns two coordinates into travel minutes.
from setuhaul.travel import StraightLineEstimator


class UnknownShipment(Exception):
    """Raised when a ping is stored for a shipment that does not exist."""


def store_ping(conn: sqlite3.Connection, shipment_id: str, ping: GpsPing, clock: Clock, source: str) -> int:
    """Insert one ping and return its ping_id; received_at is the clock's now."""
    # Refuse pings for shipments we do not know, with a clear error instead of a foreign-key failure.
    if conn.execute("SELECT 1 FROM shipments WHERE shipment_id = ?", (shipment_id,)).fetchone() is None:
        raise UnknownShipment(shipment_id)
    # Normalise both times to IST so the stored text sorts chronologically.
    recorded = ping.recorded_at.astimezone(IST).isoformat()
    received = clock.now().astimezone(IST).isoformat()
    # Insert the row; ping_id is assigned by the database.
    cur = conn.execute(
        "INSERT INTO gps_pings (shipment_id, lat, lon, recorded_at, received_at, source) VALUES (?, ?, ?, ?, ?, ?)",
        (shipment_id, ping.lat, ping.lon, recorded, received, source),
    )
    # lastrowid is the new ping_id (always set after an INSERT).
    assert cur.lastrowid is not None
    return cur.lastrowid


def active_shipment_for_driver(conn: sqlite3.Connection, driver_id: str) -> str | None:
    """The driver's open shipment with the earliest original ETA, or None if there is none."""
    # Open means neither COMPLETED nor CANCELLED; the earliest planned ETA is the one being driven.
    row = conn.execute(
        "SELECT shipment_id FROM shipments WHERE driver_id = ? AND current_status NOT IN ('COMPLETED', 'CANCELLED') "
        "ORDER BY original_eta_ts, shipment_id LIMIT 1",
        (driver_id,),
    ).fetchone()
    # No row means the driver has nothing active.
    return row["shipment_id"] if row else None


def load_latest_ping(conn: sqlite3.Connection, shipment_id: str, clock: Clock, max_age_minutes: int) -> GpsPing | None:
    """Latest ping by (recorded_at, ping_id), or None when absent or older than max_age_minutes."""
    # Latest by recorded time; the later-inserted ping wins a tie.
    row = conn.execute(
        "SELECT lat, lon, recorded_at FROM gps_pings WHERE shipment_id = ? ORDER BY recorded_at DESC, ping_id DESC LIMIT 1",
        (shipment_id,),
    ).fetchone()
    # No pings stored at all.
    if row is None:
        return None
    # Parse the stored time back into an aware datetime.
    recorded = parse_operational_timestamp(row["recorded_at"])
    # A future-dated ping counts as age zero, so clamp the age at zero.
    age = max(clock.now() - recorded, timedelta(0))
    # Too old to trust.
    if age > timedelta(minutes=max_age_minutes):
        return None
    # Fresh: hand back the validated ping.
    return GpsPing(lat=row["lat"], lon=row["lon"], recorded_at=recorded)


def gps_travel_minutes_for(conn: sqlite3.Connection, shipment_id: str, clock: Clock, settings: Settings) -> int | None:
    """Minutes from the latest fresh ping to the shipment's destination, or None if either is unavailable."""
    # Without a fresh ping there is nothing to estimate from.
    ping = load_latest_ping(conn, shipment_id, clock, settings.gps_max_age_minutes)
    if ping is None:
        return None
    # Look up the destination facility's coordinates.
    dest = conn.execute(
        "SELECT f.lat, f.lon FROM shipments s JOIN facilities f ON f.facility_id = s.destination_facility_id "
        "WHERE s.shipment_id = ?",
        (shipment_id,),
    ).fetchone()
    # Missing shipment, facility or coordinates means no estimate.
    if dest is None or dest["lat"] is None or dest["lon"] is None:
        return None
    # Straight-line estimate from the ping to the destination.
    return StraightLineEstimator(settings).estimate(ping.lat, ping.lon, dest["lat"], dest["lon"])


def gps_arrival_floor(conn: sqlite3.Connection, shipment_id: str, clock: Clock, settings: Settings) -> datetime | None:
    """The earliest the truck can reach the facility from its latest FRESH ping: now plus the straight-line drive.

    None when there is no fresh ping or no coordinates. It is used only as a floor: a slot that starts before this moment
    cannot be reached, whatever the driver said. It never makes an arrival earlier than the driver's own figure.
    """
    # Drive time from the latest fresh ping (None: nothing to go on).
    minutes = gps_travel_minutes_for(conn, shipment_id, clock, settings)
    if minutes is None:
        return None
    # The truck is somewhere right now, so the soonest it can arrive is now plus that drive.
    return clock.now() + timedelta(minutes=minutes)
