"""Durable record of exactly what a driver was shown, and reconciliation against live state.

An option set is owned by (driver, thread, shipment). A driver can only read or select a set that
matches all three; anything else looks the same as a set that does not exist.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

from setuhaul.allocation import RankedOption
from setuhaul.capacity import FacilityState, build_facility_state
from setuhaul.clock import Clock
from setuhaul.db import parse_operational_timestamp
from setuhaul.feasibility import FeasibilityContext, FeasibilityDecision, evaluate_slot
from setuhaul.identity import AuthError
from setuhaul.schemas import Aware

OPTION_TTL = timedelta(minutes=15)  # provisional (Q19): how long a shown option may be selected
RETENTION = timedelta(days=30)

Change = Literal[
    "UNCHANGED",
    "CHANGED_STILL_FEASIBLE",
    "DISAPPEARED",
    "NEWLY_BLOCKED",
    "NEWLY_HELD",
    "NEWLY_OCCUPIED",
    "NEWLY_INCOMPATIBLE",
    "NO_LONGER_FEASIBLE",
    "DATA_NOW_INCOMPLETE",
]

_BLOCKED = {"SLOT_BLOCKED", "DOCK_EVENT_BLOCK", "DOCK_UNAVAILABLE"}
_INCOMPATIBLE = {
    "DOCK_TYPE_MISMATCH", "REFRIGERATION_REQUIRED", "HEAVY_DOCK_REQUIRED",
    "WEIGHT_EXCEEDS_DOCK_LIMIT", "UNLOAD_EXCEEDS_SLOT", "OUTSIDE_FACILITY_HOURS",
}
_TEXT: dict[Change, str] = {
    "UNCHANGED": "still available",
    "CHANGED_STILL_FEASIBLE": "still available, but its details changed",
    "DISAPPEARED": "was removed from the schedule",
    "NEWLY_BLOCKED": "was blocked by the warehouse",
    "NEWLY_HELD": "is now being held for another driver",
    "NEWLY_OCCUPIED": "was booked by someone else",
    "NEWLY_INCOMPATIBLE": "no longer suits this load",
    "NO_LONGER_FEASIBLE": "can no longer be used",
    "DATA_NOW_INCOMPLETE": "cannot be confirmed right now",
}


class OptionItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    rank: int
    slot_id: str
    slot_version: str
    rules_version: str
    feasibility_hash: str
    decision: FeasibilityDecision


class OptionSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    option_set_id: str
    driver_id: str
    thread_id: str
    shipment_id: str
    facility_id: str
    shown_at: Aware
    expires_at: Aware
    rules_version: str
    context: FeasibilityContext
    items: tuple[OptionItem, ...]


class OptionDiffItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    rank: int
    slot_id: str
    change: Change
    reason_codes: tuple[str, ...] = ()
    explanation: str
    current: FeasibilityDecision | None = None


class OptionDiff(BaseModel):
    model_config = ConfigDict(frozen=True)

    option_set_id: str
    evaluated_at: Aware
    expired: bool
    rules_changed: bool
    items: tuple[OptionDiffItem, ...]

    @property
    def all_unchanged(self) -> bool:
        return not self.expired and not self.rules_changed and all(i.change == "UNCHANGED" for i in self.items)

    @property
    def still_offerable(self) -> tuple[int, ...]:
        return tuple(i.rank for i in self.items if i.change in ("UNCHANGED", "CHANGED_STILL_FEASIBLE"))


# ---- writing ---------------------------------------------------------------------------------
def record_option_set(
    conn: sqlite3.Connection,
    ctx: FeasibilityContext,
    thread_id: str,
    options: Sequence[RankedOption],
    state: FacilityState,
    clock: Clock,
    ttl: timedelta = OPTION_TTL,
) -> OptionSet:
    thread = conn.execute("select driver_id from chat_threads where thread_id=?", (thread_id,)).fetchone()
    if thread is None or thread["driver_id"] != ctx.driver_id:
        raise AuthError("FOREIGN_THREAD")
    owner = conn.execute("select driver_id from shipments where shipment_id=?", (ctx.shipment_id,)).fetchone()
    if owner is None or owner["driver_id"] != ctx.driver_id:
        raise AuthError("FOREIGN_SHIPMENT")
    now = clock.now()
    option_set_id = "OPT-" + uuid.uuid4().hex[:12].upper()
    rules_version = state.rules_version()
    with conn:
        conn.execute(
            "insert into option_sets (option_set_id, driver_id, thread_id, shipment_id, facility_id, "
            "shown_at, expires_at, rules_version, context_json) values (?,?,?,?,?,?,?,?,?)",
            (option_set_id, ctx.driver_id, thread_id, ctx.shipment_id, ctx.facility_id,
             now.isoformat(), (now + ttl).isoformat(), rules_version, ctx.model_dump_json()),
        )
        for o in options:
            d = o.decision
            conn.execute(
                "insert into option_set_items (option_set_id, displayed_rank, slot_id, slot_version, "
                "rules_version, feasibility_hash, decision_json) values (?,?,?,?,?,?,?)",
                (option_set_id, o.rank, d.slot_id, d.slot_version, rules_version, d.digest, d.model_dump_json()),
            )
    return _load(conn, option_set_id)


# ---- reading ---------------------------------------------------------------------------------
def _load(conn: sqlite3.Connection, option_set_id: str) -> OptionSet:
    head = conn.execute("select * from option_sets where option_set_id=?", (option_set_id,)).fetchone()
    rows = conn.execute(
        "select * from option_set_items where option_set_id=? order by displayed_rank", (option_set_id,)
    ).fetchall()
    return OptionSet(
        option_set_id=head["option_set_id"],
        driver_id=head["driver_id"],
        thread_id=head["thread_id"],
        shipment_id=head["shipment_id"],
        facility_id=head["facility_id"],
        shown_at=parse_operational_timestamp(head["shown_at"]),
        expires_at=parse_operational_timestamp(head["expires_at"]),
        rules_version=head["rules_version"],
        context=FeasibilityContext.model_validate_json(head["context_json"]),
        items=tuple(
            OptionItem(
                rank=r["displayed_rank"], slot_id=r["slot_id"], slot_version=r["slot_version"],
                rules_version=r["rules_version"], feasibility_hash=r["feasibility_hash"],
                decision=FeasibilityDecision.model_validate_json(r["decision_json"]),
            )
            for r in rows
        ),
    )


def get_option_set(
    conn: sqlite3.Connection, driver_id: str, thread_id: str, shipment_id: str, option_set_id: str
) -> OptionSet:
    row = conn.execute(
        "select option_set_id from option_sets where option_set_id=? and driver_id=? and thread_id=? "
        "and shipment_id=?",
        (option_set_id, driver_id, thread_id, shipment_id),
    ).fetchone()
    if row is None:
        raise AuthError("FOREIGN_OPTION_SET")
    return _load(conn, option_set_id)


def get_latest_option_set(
    conn: sqlite3.Connection, driver_id: str, thread_id: str, shipment_id: str
) -> OptionSet | None:
    row = conn.execute(
        "select option_set_id from option_sets where driver_id=? and thread_id=? and shipment_id=? "
        "order by shown_at desc, rowid desc limit 1",
        (driver_id, thread_id, shipment_id),
    ).fetchone()
    return _load(conn, row["option_set_id"]) if row else None


# ---- reconciliation --------------------------------------------------------------------------
def _classify(item: OptionItem, cur: FeasibilityDecision) -> tuple[Change, tuple[str, ...]]:
    if cur.status == "FEASIBLE":
        same = cur.slot_version == item.slot_version and cur.digest == item.feasibility_hash
        return ("UNCHANGED" if same else "CHANGED_STILL_FEASIBLE"), ()
    reasons = set(cur.reasons)
    codes = cur.reasons
    if "SLOT_HELD" in reasons:
        return "NEWLY_HELD", codes
    if "SLOT_OCCUPIED" in reasons:
        return "NEWLY_OCCUPIED", codes
    if reasons & _BLOCKED:
        return "NEWLY_BLOCKED", codes
    if reasons & _INCOMPATIBLE:
        return "NEWLY_INCOMPATIBLE", codes
    if cur.status == "INCOMPLETE_FEASIBILITY_DATA":
        return "DATA_NOW_INCOMPLETE", codes
    return "NO_LONGER_FEASIBLE", codes


def diff_option_set(option_set: OptionSet, now: datetime, state: FacilityState) -> OptionDiff:
    """Pure core: compare what was shown with ``state`` as of ``now``."""
    ctx = option_set.context.with_now(now)
    items: list[OptionDiffItem] = []
    for it in option_set.items:
        slot = state.slots.get(it.slot_id)
        if slot is None:
            items.append(OptionDiffItem(rank=it.rank, slot_id=it.slot_id, change="DISAPPEARED",
                                        explanation=f"Option {it.rank} {_TEXT['DISAPPEARED']}."))
            continue
        cur = evaluate_slot(ctx, slot, state)
        change, codes = _classify(it, cur)
        items.append(OptionDiffItem(rank=it.rank, slot_id=it.slot_id, change=change, reason_codes=codes,
                                    explanation=f"Option {it.rank} {_TEXT[change]}.", current=cur))
    return OptionDiff(
        option_set_id=option_set.option_set_id, evaluated_at=now, expired=now >= option_set.expires_at,
        rules_changed=state.rules_version() != option_set.rules_version, items=tuple(items),
    )


def reconcile_option_set(conn: sqlite3.Connection, option_set: OptionSet, clock: Clock) -> OptionDiff:
    """Compare a shown option set with live state. Pass a set obtained through ``get_option_set`` so
    ownership has already been proven."""
    now = clock.now()
    state = build_facility_state(conn, option_set.facility_id, now)
    return diff_option_set(option_set, now, state)


# ---- retention -------------------------------------------------------------------------------
def purge_option_sets(conn: sqlite3.Connection, before: datetime) -> int:
    """Delete sets shown before ``before``. Compared as instants, never as strings."""
    stale = [
        r["option_set_id"]
        for r in conn.execute("select option_set_id, shown_at from option_sets")
        if parse_operational_timestamp(r["shown_at"]) < before
    ]
    with conn:
        for option_set_id in stale:
            conn.execute("delete from option_sets where option_set_id=?", (option_set_id,))
    return len(stale)
