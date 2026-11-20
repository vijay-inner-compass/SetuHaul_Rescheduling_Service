"""GPS data foundation: migration, settings defaults and the GpsPing schema."""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest
from pydantic import ValidationError

from setuhaul.config import Settings
from setuhaul.schemas import GpsPing


# The migration adds the ping table and gives each facility coordinates.
def test_migration_creates_gps_pings_and_facility_coordinates(db) -> None:
    # The table exists after migrations have run on the working copy.
    tables = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
    assert "gps_pings" in tables
    # Jaipur has non-null coordinates close to the city centre.
    lat, lon = db.execute("select lat, lon from facilities where facility_id='FAC-JAI-01'").fetchone()
    assert lat is not None and lon is not None
    assert abs(lat - 26.91) < 1 and abs(lon - 75.79) < 1


# Out-of-range coordinates and naive timestamps are rejected at the schema boundary.
def test_gps_ping_rejects_out_of_range_and_naive_time() -> None:
    aware = datetime.fromisoformat("2026-08-04T09:00:00+05:30")
    # A valid ping is accepted.
    GpsPing(lat=26.9, lon=75.8, recorded_at=aware)
    # Latitude above 90 is invalid.
    with pytest.raises(ValidationError):
        GpsPing(lat=91, lon=75.8, recorded_at=aware)
    # Longitude above 180 is invalid.
    with pytest.raises(ValidationError):
        GpsPing(lat=26.9, lon=181, recorded_at=aware)
    # A timestamp without a timezone is invalid.
    with pytest.raises(ValidationError):
        GpsPing(lat=26.9, lon=75.8, recorded_at=datetime(2026, 8, 4, 9, 0))


# The four GPS tunables carry the spec defaults.
def test_settings_defaults_match_spec(tmp_path) -> None:
    s = Settings(db_path=tmp_path / "x.sqlite3")
    assert s.gps_max_age_minutes == 15
    assert s.gps_mismatch_minutes == 30
    assert s.gps_avg_speed_kmph == 40.0
    assert s.gps_road_factor == 1.3


# Helper: insert one ping row directly, bypassing the Pydantic schema, to test the SQL CHECKs.
def _insert_ping(db, lat: float, lon: float) -> None:
    db.execute(
        "insert into gps_pings (shipment_id, lat, lon, recorded_at, received_at, source) "
        "values ('SHP1006', ?, ?, '2026-08-04T09:00:00+05:30', '2026-08-04T09:00:05+05:30', 'TEST')",
        (lat, lon),
    )


# The database itself rejects impossible coordinates, whatever the caller does.
@pytest.mark.parametrize("lat,lon", [(91, 75.8), (-91, 75.8), (26.9, 181), (26.9, -181)])
def test_gps_pings_check_rejects_out_of_range(db, lat: float, lon: float) -> None:
    # A savepoint keeps the connection usable after the failed insert.
    db.execute("savepoint s")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_ping(db, lat, lon)
    db.execute("rollback to s")
    # Nothing was stored.
    assert db.execute("select count(*) from gps_pings").fetchone()[0] == 0


# The exact boundary values are valid.
def test_gps_pings_check_accepts_boundaries(db) -> None:
    for lat, lon in [(90, 180), (-90, -180), (90, -180), (-90, 180)]:
        _insert_ping(db, lat, lon)
    assert db.execute("select count(*) from gps_pings").fetchone()[0] == 4
