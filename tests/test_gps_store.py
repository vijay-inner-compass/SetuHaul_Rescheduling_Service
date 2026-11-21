"""Tests for GPS ping storage, freshness and travel-time lookup."""

# Lazy annotations.
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from setuhaul.clock import FakeClock
from setuhaul.config import Settings
from setuhaul.db import parse_operational_timestamp as ts
from setuhaul.gps import (
    UnknownShipment,
    active_shipment_for_driver,
    gps_travel_minutes_for,
    load_latest_ping,
    store_ping,
)
from setuhaul.schemas import GpsPing
from setuhaul.travel import StraightLineEstimator, estimate_travel_minutes

# Jaipur facility coordinates seeded by migration 009.
JAI = (26.9124, 75.7873)


def _ping(clock: FakeClock, minutes_ago: float, lat: float = 27.0, lon: float = 75.8) -> GpsPing:
    # Build a ping recorded the given number of minutes before the fake clock's now.
    return GpsPing(lat=lat, lon=lon, recorded_at=clock.now() - timedelta(minutes=minutes_ago))


def test_store_and_read_back(db: sqlite3.Connection, clock: FakeClock) -> None:
    # Store one ping and read it back.
    pid = store_ping(db, "SHP1006", _ping(clock, 2), clock, "TELEMATICS")
    assert isinstance(pid, int)
    got = load_latest_ping(db, "SHP1006", clock, 15)
    assert got is not None and (got.lat, got.lon) == (27.0, 75.8)
    # received_at is the clock's now and source is kept verbatim.
    row = db.execute("SELECT received_at, source FROM gps_pings WHERE ping_id=?", (pid,)).fetchone()
    assert ts(row["received_at"]) == clock.now() and row["source"] == "TELEMATICS"


def test_unknown_shipment_raises(db: sqlite3.Connection, clock: FakeClock) -> None:
    with pytest.raises(UnknownShipment):
        store_ping(db, "NOPE", _ping(clock, 1), clock, "TELEMATICS")


def test_freshness_boundaries(db: sqlite3.Connection, clock: FakeClock) -> None:
    store_ping(db, "SHP1006", _ping(clock, 14), clock, "TELEMATICS")
    assert load_latest_ping(db, "SHP1006", clock, 15) is not None
    # A ping 16 minutes old is stale.
    db.execute("DELETE FROM gps_pings")
    store_ping(db, "SHP1006", _ping(clock, 16), clock, "TELEMATICS")
    assert load_latest_ping(db, "SHP1006", clock, 15) is None


def test_no_ping_returns_none(db: sqlite3.Connection, clock: FakeClock) -> None:
    assert load_latest_ping(db, "SHP1006", clock, 15) is None


def test_future_dated_ping_counts_as_age_zero(db: sqlite3.Connection, clock: FakeClock) -> None:
    store_ping(db, "SHP1006", _ping(clock, -30), clock, "TELEMATICS")
    assert load_latest_ping(db, "SHP1006", clock, 15) is not None


def test_same_timestamp_later_inserted_wins(db: sqlite3.Connection, clock: FakeClock) -> None:
    store_ping(db, "SHP1006", _ping(clock, 3, lat=10.0), clock, "TELEMATICS")
    store_ping(db, "SHP1006", _ping(clock, 3, lat=11.0), clock, "TELEMATICS")
    got = load_latest_ping(db, "SHP1006", clock, 15)
    assert got is not None and got.lat == 11.0


def test_mixed_offsets_order_chronologically(db: sqlite3.Connection, clock: FakeClock) -> None:
    # now is 09:35 IST. early is 09:30+05:30 (04:00Z); late is 04:04Z (09:34 IST) but its UTC text sorts earlier.
    early_ist = GpsPing(lat=1.0, lon=1.0, recorded_at=ts("2026-08-04T09:30:00+05:30"))
    late_utc = GpsPing(lat=2.0, lon=2.0, recorded_at=datetime(2026, 8, 4, 4, 4, tzinfo=UTC))
    store_ping(db, "SHP1006", early_ist, clock, "TELEMATICS")
    store_ping(db, "SHP1006", late_utc, clock, "TELEMATICS")
    got = load_latest_ping(db, "SHP1006", clock, 15)
    assert got is not None and got.lat == 2.0
    # Every stored timestamp carries the IST offset.
    for row in db.execute("SELECT recorded_at, received_at FROM gps_pings"):
        assert row["recorded_at"].endswith("+05:30") and row["received_at"].endswith("+05:30")


def test_active_shipment_earliest_eta(db: sqlite3.Connection) -> None:
    # DRV003 has two open shipments; the earlier original ETA among open ones wins.
    rows = db.execute(
        "SELECT shipment_id FROM shipments WHERE driver_id='DRV003' "
        "AND current_status NOT IN ('COMPLETED','CANCELLED') ORDER BY original_eta_ts"
    ).fetchall()
    assert len(rows) >= 2
    assert active_shipment_for_driver(db, "DRV003") == rows[0]["shipment_id"]
    # Completing the first makes the second active.
    db.execute("UPDATE shipments SET current_status='COMPLETED' WHERE shipment_id=?", (rows[0]["shipment_id"],))
    assert active_shipment_for_driver(db, "DRV003") == rows[1]["shipment_id"]


def test_active_shipment_none_when_all_closed(db: sqlite3.Connection) -> None:
    db.execute("UPDATE shipments SET current_status='COMPLETED' WHERE driver_id='DRV001'")
    db.execute("UPDATE shipments SET current_status='CANCELLED' WHERE shipment_id=(SELECT MIN(shipment_id) FROM shipments WHERE driver_id='DRV001')")
    assert active_shipment_for_driver(db, "DRV001") is None
    assert active_shipment_for_driver(db, "NO-SUCH-DRIVER") is None


def test_gps_travel_minutes_matches_estimator(db: sqlite3.Connection, clock: FakeClock, tmp_path: Path) -> None:
    settings = Settings(db_path=tmp_path / "x.sqlite3")
    # 60 km is about 0.5396 degrees of latitude; place the ping that far north of the facility.
    lat = JAI[0] + 60 / 111.195
    store_ping(db, "SHP1006", _ping(clock, 1, lat=lat, lon=JAI[1]), clock, "TELEMATICS")
    expected = estimate_travel_minutes(
        lat, JAI[1], JAI[0], JAI[1],
        avg_speed_kmph=settings.gps_avg_speed_kmph, road_factor=settings.gps_road_factor,
    )
    assert gps_travel_minutes_for(db, "SHP1006", clock, settings) == expected
    assert expected == StraightLineEstimator(settings).estimate(lat, JAI[1], JAI[0], JAI[1])


def test_travel_minutes_none_without_fresh_ping(db: sqlite3.Connection, clock: FakeClock, tmp_path: Path) -> None:
    settings = Settings(db_path=tmp_path / "x.sqlite3")
    assert gps_travel_minutes_for(db, "SHP1006", clock, settings) is None
    store_ping(db, "SHP1006", _ping(clock, settings.gps_max_age_minutes + 5), clock, "TELEMATICS")
    assert gps_travel_minutes_for(db, "SHP1006", clock, settings) is None


def test_travel_minutes_none_when_facility_has_no_coordinates(db: sqlite3.Connection, clock: FakeClock, tmp_path: Path) -> None:
    settings = Settings(db_path=tmp_path / "x.sqlite3")
    store_ping(db, "SHP1006", _ping(clock, 1), clock, "TELEMATICS")
    db.execute("UPDATE facilities SET lat=NULL, lon=NULL WHERE facility_id='FAC-JAI-01'")
    assert gps_travel_minutes_for(db, "SHP1006", clock, settings) is None
