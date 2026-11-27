"""Blocking a dock for a period and reopening it.

A block is an ordinary dock event, so everything that judges a slot already respects it. `dock_blocks` records who did it,
and ends one block early without changing the event or touching the other blocks on that dock.
"""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from setuhaul.auth import audit
from setuhaul.auth.authz import facility_ids
from setuhaul.auth.shipments import _facility, _ist
from setuhaul.auth.users import User
from setuhaul.capacity import BLOCKING_EVENT_TYPES, build_facility_state
from setuhaul.clock import Clock
from setuhaul.db import IST, immediate, parse_operational_timestamp
from setuhaul.identity import AuthError


class BlockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facility_id: str
    dock_id: str
    event_type: Literal["MANUAL_BLOCK", "MAINTENANCE", "BREAKDOWN"]
    starts: datetime
    # Empty: until someone reopens it.
    ends: datetime | None = None
    reason: str = Field(min_length=1, max_length=200)


def _checked(conn: sqlite3.Connection, actor: User, data: BlockRequest, clock: Clock) -> tuple[datetime, datetime | None]:
    """The block's times in India time, if this user may block this dock for them."""
    _facility(conn, actor, data.facility_id)
    if not conn.execute("select 1 from docks where dock_id=? and facility_id=?", (data.dock_id, data.facility_id)).fetchone():
        raise AuthError("DOCK_NOT_FOUND")
    if not data.reason.strip():
        raise AuthError("REASON_REQUIRED")
    starts, ends = _ist(data.starts), None if data.ends is None else _ist(data.ends)
    if ends is not None and (ends <= starts or ends <= clock.now()):
        raise AuthError("INVALID_TIMES")
    return starts, ends


def insert_block_in_txn(conn: sqlite3.Connection, actor: User, data: BlockRequest, clock: Clock) -> str:
    """Store the block and return its event id. Call inside a transaction."""
    starts, ends = _checked(conn, actor, data, clock)
    event_id, stamp = "DEVT-" + uuid.uuid4().hex[:12].upper(), clock.now().astimezone(IST).isoformat()
    conn.execute("insert into dock_status_events(dock_event_id, dock_id, event_type, event_start_ts, event_end_ts, reason,"
                 " created_at) values (?,?,?,?,?,?,?)",
                 (event_id, data.dock_id, data.event_type, starts.isoformat(), None if ends is None else ends.isoformat(),
                  data.reason.strip(), stamp))
    conn.execute("insert into dock_blocks(dock_event_id, created_by, created_at) values (?,?,?)", (event_id, actor.user_id, stamp))
    audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action="DOCK_BLOCKED", target_type="dock",
                 target_id=data.dock_id, outcome="OK", ip=None, detail={"dock_event_id": event_id}, clock=clock)
    return event_id


def current(conn: sqlite3.Connection, facility_ids: Sequence[str], now: datetime) -> list[dict[str, Any]]:
    """Blocks in force or still to come at these facilities, supplied ones included, earliest first."""
    marks = ",".join("?" for _ in facility_ids) or "null"
    rows = conn.execute(
        "select e.dock_event_id, e.dock_id, k.dock_code, k.facility_id, e.event_type, e.event_start_ts as starts,"
        " e.event_end_ts as ends, e.reason, b.ended_at from dock_status_events e join docks k using(dock_id)"
        f" left join dock_blocks b using(dock_event_id) where k.facility_id in ({marks})", tuple(facility_ids)).fetchall()
    out = []
    for r in rows:
        # Reopened here, or already over, or closed by a supplied REOPENED event.
        if r["event_type"] not in BLOCKING_EVENT_TYPES or r["ended_at"] is not None:
            continue
        reopened = conn.execute("select 1 from dock_status_events where dock_id=? and event_type='REOPENED'"
                                " and event_start_ts>=? and event_start_ts<=?",
                                (r["dock_id"], r["starts"], now.astimezone(IST).isoformat())).fetchone()
        if reopened or (r["ends"] is not None and parse_operational_timestamp(r["ends"]) <= now):
            continue
        out.append({k: r[k] for k in ("dock_event_id", "dock_id", "dock_code", "facility_id", "event_type", "starts", "ends",
                                      "reason")})
    return sorted(out, key=lambda b: (parse_operational_timestamp(b["starts"]), b["dock_id"]))


def reopen(conn: sqlite3.Connection, *, actor: User, dock_event_id: str, clock: Clock) -> None:
    """End one block now. A block that had not started never takes effect."""
    with immediate(conn):
        scope = facility_ids(actor)
        mine = [r[0] for r in conn.execute("select facility_id from facilities where is_partner=0")
                if actor.status == "ACTIVE" and (scope is None or r[0] in scope)]
        block = next((b for b in current(conn, mine, clock.now()) if b["dock_event_id"] == dock_event_id), None)
        if block is None:
            raise AuthError("BLOCK_NOT_FOUND")
        now, stamp = clock.now(), clock.now().astimezone(IST).isoformat()
        conn.execute("insert into dock_blocks(dock_event_id, created_at, ended_by, ended_at) values (?,?,?,?)"
                     " on conflict(dock_event_id) do update set ended_by=excluded.ended_by, ended_at=excluded.ended_at",
                     (dock_event_id, stamp, actor.user_id, stamp))
        # Supplied blocks are also written onto the slots themselves; those open again unless another block still covers them.
        state = build_facility_state(conn, block["facility_id"], now)
        starts = parse_operational_timestamp(block["starts"])
        ends = None if block["ends"] is None else parse_operational_timestamp(block["ends"])
        others = state.effective_events(block["dock_id"])
        for slot in state.slots.values():
            if (slot.dock_id != block["dock_id"] or slot.status != "BLOCKED" or slot.end <= now or slot.end <= starts
                    or (ends is not None and slot.start >= ends)
                    or any(e.start < slot.end and (e.end is None or e.end > max(slot.start, now)) for e in others)):
                continue
            conn.execute("update appointment_slots set slot_status='OPEN', block_reason=null where slot_id=?", (slot.slot_id,))
        audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action="DOCK_REOPENED", target_type="dock",
                     target_id=block["dock_id"], outcome="OK", ip=None, detail={"dock_event_id": dock_event_id}, clock=clock)
