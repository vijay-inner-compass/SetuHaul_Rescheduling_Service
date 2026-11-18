"""Phase 0 baseline: interpreter, timezone, seed database presence/integrity, schema."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from setuhaul.config import SEED_DB_SHA256, load_settings

EXPECTED_TABLES = {
    "appointment_slots", "appointments", "carriers", "chat_messages", "chat_threads",
    "dock_status_events", "docks", "driver_exceptions", "drivers", "eta_updates",
    "facilities", "facility_checkins", "facility_contacts", "facility_rules",
    "operational_messages", "shipments", "vehicle_types", "vehicles",
}
EXPECTED_VIEWS = {
    "v_current_facility_queue", "v_inbound_operational_state",
    "v_latest_eta", "v_slot_availability",
}


def _readonly():
    settings = load_settings()
    assert settings.db_path.exists(), f"missing database {settings.db_path}; do not synthesize"
    return sqlite3.connect(f"file:{settings.db_path.as_posix()}?mode=ro", uri=True)


def test_python_version() -> None:
    assert sys.version_info[:2] == (3, 12)


def test_operational_timezone_resolves() -> None:
    assert load_settings().timezone == "Asia/Kolkata"
    offset = datetime(2026, 1, 1, tzinfo=ZoneInfo("Asia/Kolkata")).utcoffset()
    assert offset == timedelta(hours=5, minutes=30)


def test_database_present_and_hash_matches_supplied_seed() -> None:
    digest = hashlib.sha256(load_settings().db_path.read_bytes()).hexdigest().upper()
    assert digest == SEED_DB_SHA256


def test_read_only_schema_enumeration() -> None:
    with _readonly() as conn:
        rows = conn.execute("select type, name from sqlite_master").fetchall()
    assert {n for t, n in rows if t == "table" and not n.startswith("sqlite_")} == EXPECTED_TABLES
    assert {n for t, n in rows if t == "view"} == EXPECTED_VIEWS
