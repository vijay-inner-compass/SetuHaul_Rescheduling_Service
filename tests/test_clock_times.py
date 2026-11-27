"""A time of day the driver states is the next sensible one, not always today's.

At 23:30 "I will reach at 01:00" means tomorrow. Reading it as today offered a slot the truck could not reach.
"""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from setuhaul import round_clock
from setuhaul.clock import FakeClock
from setuhaul.db import parse_operational_timestamp as ts
from setuhaul.tools import arrival_time, time_near
from tests.auth_helpers import add_user
from tests.conv_helpers import Chat, say_text, tc


@pytest.mark.parametrize("now,said,expected", [
    ("2026-08-04T23:30", "01:00", "2026-08-05T01:00"),  # after midnight
    ("2026-08-04T09:35", "11:20", "2026-08-04T11:20"),  # later today
    ("2026-08-04T09:35", "09:00", "2026-08-04T09:00"),  # just gone: the truck is arriving now
    ("2026-08-04T09:35", "08:35", "2026-08-04T08:35"),  # exactly one hour ago still counts as now
    ("2026-08-04T09:35", "08:34", "2026-08-05T08:34"),  # longer ago than that is tomorrow
])
def test_an_arrival_time_is_never_read_as_long_past(now: str, said: str, expected: str) -> None:
    assert arrival_time(_at(now), said) == _at(expected)


@pytest.mark.parametrize("eta,said,expected", [
    ("2026-08-05T01:00", "03:00", "2026-08-05T03:00"),  # limit after an after-midnight arrival
    ("2026-08-04T11:00", "08:00", "2026-08-04T08:00"),  # "not before 8" for an 11:00 arrival is today
    ("2026-08-04T22:00", "02:00", "2026-08-05T02:00"),  # "not after 2" for a 22:00 arrival is tonight
    ("2026-08-05T00:30", "23:00", "2026-08-04T23:00"),  # "from 11 pm" for an arrival just after midnight
])
def test_a_slot_limit_is_the_occurrence_nearest_the_arrival(eta: str, said: str, expected: str) -> None:
    assert time_near(_at(eta), said) == _at(expected)


def _at(text: str) -> datetime:
    return ts(f"{text}:00+05:30")


def test_arriving_after_midnight_is_offered_a_slot_after_arrival(db: sqlite3.Connection, tmp_path: Path) -> None:
    clock = FakeClock(_at("2026-08-04T23:30"))
    admin = add_user(db, clock, "night@example.test", "ADMIN")
    round_clock.open_all_hours(db)
    round_clock.set_crews(db, actor=admin, facility_id="FAC-JAI-01", crews=8, clock=clock)
    db.execute("insert into consignee_commitments(shipment_id, latest_arrival, penalty_applies, exceptional, source, recorded_at)"
               " values ('SHP1006', null, 0, 0, 'TEST_FIXTURE', ?)", (clock.now().isoformat(),))
    db.commit()
    chat = Chat(db, clock, tmp_path)
    chat.say("SHP1006 I will reach at 01:00", [
        tc("search_slot_options", shipment_id="SHP1006", report={"kind": "explicit_eta", "eta_time": "01:00"}), say_text("x")])
    held = db.execute("select s.slot_start_ts from appointments a join appointment_slots s using(slot_id) "
                      "where a.appointment_status='HELD'").fetchone()[0]
    assert _at("2026-08-05T01:00") <= ts(held)
