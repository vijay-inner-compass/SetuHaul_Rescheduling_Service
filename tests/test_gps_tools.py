"""Tool-level tests: GPS travel time flows through compute_eta into the ETA payload."""

# Lazy annotations.
from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from setuhaul.clock import FakeClock
from setuhaul.gps import store_ping
from setuhaul.schemas import GpsPing
from tests.conv_helpers import Chat, say_text, tc, tool_json
from tests.helpers import seed_everything_known

# A repair report with no travel time, which normally forces a question to the driver.
REPAIR = {"kind": "repair", "minutes": 90}


@pytest.fixture()
def chat(db: sqlite3.Connection, clock: FakeClock, tmp_path: Path) -> Chat:
    # A conversation with the shared seed data loaded.
    seed_everything_known(db)
    return Chat(db, clock, tmp_path)


def _ping(chat: Chat, minutes_ago: float, lat: float = 27.0, lon: float = 75.8) -> None:
    # Store a ping recorded the given number of minutes before the fake clock's now (default: near the Jaipur facility).
    ping = GpsPing(lat=lat, lon=lon, recorded_at=chat.clock.now() - timedelta(minutes=minutes_ago))
    store_ping(chat.db, "SHP1006", ping, chat.clock, "TEST")


def _compute_eta(chat: Chat, report: dict[str, Any]) -> dict[str, Any]:
    # Drive one compute_eta call and capture the payload the model sees.
    seen: dict[str, Any] = {}

    def grab(msgs):  # type: ignore[no-untyped-def]
        seen.update(tool_json(msgs, "compute_eta"))
        return say_text("ok")

    chat.say("SHP1006 is running late, please work out my ETA", [tc("compute_eta", shipment_id="SHP1006", report=report), grab])
    return seen


def test_fresh_ping_fills_missing_travel_time(chat: Chat) -> None:
    # A fresh ping stands in for the driver's missing travel time.
    _ping(chat, 2)
    p = _compute_eta(chat, REPAIR)
    assert p["status"] == "ETA" and p["source"] == "GPS_ASSISTED"
    # No clarification is requested and no discrepancy is reported.
    assert "ask_the_driver" not in p and "gps_discrepancy_minutes" not in p


def test_stale_ping_still_asks_for_travel_time(chat: Chat) -> None:
    # A ping older than 15 minutes is ignored, so the existing clarification stands.
    _ping(chat, 20)
    p = _compute_eta(chat, REPAIR)
    assert p["status"] == "NEEDS_INPUT" and p["code"] == "MISSING_TRAVEL_TIME"


def test_far_ping_flags_discrepancy_on_late_by(chat: Chat) -> None:
    # A truck pinged near Delhi cannot be only 5 minutes late; the gap is reported.
    _ping(chat, 1, lat=28.6, lon=77.2)
    p = _compute_eta(chat, {"kind": "late_by", "minutes": 5})
    assert p["status"] == "ETA" and isinstance(p["gps_discrepancy_minutes"], int) and p["gps_discrepancy_minutes"] > 0


def test_no_ping_payload_unchanged(chat: Chat) -> None:
    # Without any ping the payload keeps today's shape.
    p = _compute_eta(chat, {"kind": "late_by", "minutes": 5})
    assert p["status"] == "ETA" and "gps_discrepancy_minutes" not in p
    assert set(p) == {"status", "eta", "source", "confidence", "baseline"}
