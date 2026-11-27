"""Warehouses open all day and night, and their dock slots created ahead automatically.

The service opens every warehouse's hours at start-up. Slots are created only for a warehouse whose staff have typed how
many crews it has: crew numbers are never assumed.
"""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime, time, timedelta

from setuhaul.auth import audit
from setuhaul.auth.authz import facility_ids
from setuhaul.auth.users import User
from setuhaul.clock import Clock
from setuhaul.db import IST, immediate, parse_operational_timestamp
from setuhaul.identity import AuthError

# Opening and closing at midnight means open all day.
ALL_DAY = "00:00"
# Slots exist for today and the next 13 days.
DAYS_AHEAD = 14
SLOT = timedelta(hours=1)
# Labour windows made here, so they are never mistaken for supplied or typed ones.
SOURCE = "AUTO"


def open_all_hours(conn: sqlite3.Connection) -> None:
    """Every warehouse is open 24 hours, with no cut-off for new starts."""
    with immediate(conn):
        conn.execute("update facilities set open_time=?, close_time=?", (ALL_DAY, ALL_DAY))
        conn.execute("update facility_rules set active_flag=0 where rule_type='LAST_NEW_START_TIME'")


def crews_of(conn: sqlite3.Connection, facility_id: str) -> int | None:
    row = conn.execute("select crews from facility_crews where facility_id=?", (facility_id,)).fetchone()
    return None if row is None else int(row[0])


def _fill_day(conn: sqlite3.Connection, facility_id: str, crews: int, docks: list[str], day: date, now: datetime) -> int:
    start = datetime.combine(day, time(), tzinfo=IST)
    end, stamp = start + timedelta(days=1), now.isoformat()
    # Labour already recorded is left as it is; only the hours nothing describes get a window.
    covered = sorted((parse_operational_timestamp(r[0]), parse_operational_timestamp(r[1])) for r in conn.execute(
        "select window_start, window_end from labour_windows where facility_id=? and substr(window_start,1,10)=?",
        (facility_id, day.isoformat())))
    gaps, at = [], start
    for first, last in covered:
        if first > at:
            gaps.append((at, first))
        at = max(at, last)
    if at < end:
        gaps.append((at, end))
    for first, last in gaps:
        conn.execute("insert into labour_windows(facility_id, window_start, window_end, available_crews, equipment_available,"
                     " source, recorded_at) values (?,?,?,?,1,?,?)",
                     (facility_id, first.astimezone(IST).isoformat(), last.astimezone(IST).isoformat(), crews, SOURCE, stamp))
    created = 0
    for dock in docks:
        at = start
        while at < end:
            until = at + SLOT
            # Any slot overlapping this hour on this dock means it is already described.
            if at > now and not conn.execute("select 1 from appointment_slots where dock_id=? and slot_start_ts<? and slot_end_ts>?",
                                             (dock, until.isoformat(), at.isoformat())).fetchone():
                conn.execute("insert into appointment_slots(slot_id, facility_id, dock_id, slot_start_ts, slot_end_ts, slot_status,"
                             " created_at) values (?,?,?,?,?,'OPEN',?)",
                             ("SL-" + uuid.uuid4().hex[:10].upper(), facility_id, dock, at.isoformat(), until.isoformat(), stamp))
                created += 1
            at = until
    return created


def _fill(conn: sqlite3.Connection, facility_id: str, crews: int, now: datetime) -> int:
    """Create what is missing for one warehouse. Call inside a transaction."""
    docks = [r[0] for r in conn.execute("select dock_id from docks where facility_id=? and dock_status='ACTIVE' order by dock_id",
                                        (facility_id,))]
    today = now.astimezone(IST).date()
    return sum(_fill_day(conn, facility_id, crews, docks, today + timedelta(days=n), now) for n in range(DAYS_AHEAD))


def top_up(conn: sqlite3.Connection, clock: Clock) -> int:
    """Create the missing slots of every warehouse with a crew number. Returns how many were created."""
    rows = conn.execute("select c.facility_id, c.crews from facility_crews c join facilities f using(facility_id)"
                        " where f.active_flag=1 and f.is_partner=0").fetchall()
    created = 0
    for row in rows:
        with immediate(conn):
            created += _fill(conn, row["facility_id"], row["crews"], clock.now())
    return created


def set_crews(conn: sqlite3.Connection, *, actor: User, facility_id: str, crews: int, clock: Clock) -> int:
    """Record the warehouse's crew number and create its slots. Returns how many were created."""
    with immediate(conn):
        scope = facility_ids(actor)
        row = conn.execute("select is_partner from facilities where facility_id=? and active_flag=1", (facility_id,)).fetchone()
        if actor.status != "ACTIVE" or row is None or (scope is not None and facility_id not in scope):
            raise AuthError("FACILITY_NOT_FOUND")
        if row["is_partner"]:
            raise AuthError("PARTNER_FACILITY")
        now = clock.now()
        conn.execute("insert into facility_crews(facility_id, crews, updated_by, updated_at) values (?,?,?,?)"
                     " on conflict(facility_id) do update set crews=excluded.crews, updated_by=excluded.updated_by,"
                     " updated_at=excluded.updated_at", (facility_id, crews, actor.user_id, now.isoformat()))
        # A changed number applies to the automatic windows still ahead.
        conn.execute("update labour_windows set available_crews=? where facility_id=? and source=? and julianday(window_end)>julianday(?)",
                     (crews, facility_id, SOURCE, now.isoformat()))
        created = _fill(conn, facility_id, crews, now)
        audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action="CREWS_SET", target_type="facility",
                     target_id=facility_id, outcome="OK", ip=None, detail={"crews": crews, "slots": created}, clock=clock)
    return created
