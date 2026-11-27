"""Durable record of exactly what a driver was shown, and reconciliation against live state.

An option set is owned by (driver, thread, shipment). A driver can only read or select a set that
matches all three; anything else looks the same as a set that does not exist.
"""

# Lazy annotations.
from __future__ import annotations

# sqlite3 for the Connection type.
import sqlite3

# uuid makes unique option set ids.
import uuid

# Sequence for read-only list parameters.
from collections.abc import Sequence

# datetime/timedelta for times and lifetimes.
from datetime import datetime, timedelta

# Literal restricts the change categories to a fixed list.
from typing import Literal

# Frozen validated models.
from pydantic import BaseModel, ConfigDict

# One ranked line of a menu.
from setuhaul.allocation import RankedOption

# The facility snapshot and its loader.
from setuhaul.capacity import FacilityState, build_facility_state

# The injectable clock.
from setuhaul.clock import Clock

# Settings give the GPS limits.
from setuhaul.config import load_settings

# Timestamp parsing.
from setuhaul.db import parse_operational_timestamp

# Re-evaluating a slot uses the same rules as the original search.
from setuhaul.feasibility import FeasibilityContext, FeasibilityDecision, evaluate_slot

# The authorisation error type.
from setuhaul.gps import gps_arrival_floor

# The authorisation error type.
from setuhaul.identity import AuthError

# Timezone-aware timestamp type.
from setuhaul.schemas import Aware

OPTION_TTL = timedelta(minutes=15)  # provisional (Q19): how long a shown option may be selected
# Shown options are kept this long for audit, then purged by the maintenance job.
RETENTION = timedelta(days=30)

# Every way a shown option can differ from live reality when the driver comes back to it.
Change = Literal[
    "UNCHANGED",
    "CHANGED_STILL_FEASIBLE",
    "DISAPPEARED",
    "NEWLY_BLOCKED",
    "NEWLY_HELD",
    "NEWLY_OCCUPIED",
    "NEWLY_INCOMPATIBLE",
    "NEWLY_UNREACHABLE",
    "NO_LONGER_FEASIBLE",
    "DATA_NOW_INCOMPLETE",
]

# Rejection reasons that mean "the warehouse closed this slot or dock".
_BLOCKED = {"SLOT_BLOCKED", "DOCK_EVENT_BLOCK", "DOCK_UNAVAILABLE"}
# Rejection reasons that mean "this slot no longer suits this load".
_INCOMPATIBLE = {
    "DOCK_TYPE_MISMATCH", "REFRIGERATION_REQUIRED", "HEAVY_DOCK_REQUIRED",
    "WEIGHT_EXCEEDS_DOCK_LIMIT", "UNLOAD_EXCEEDS_SLOT", "OUTSIDE_FACILITY_HOURS",
}
# Plain-language wording for each change, used in explanations shown to the driver or in tool results.
_TEXT: dict[Change, str] = {
    "UNCHANGED": "still available",
    "CHANGED_STILL_FEASIBLE": "still available, but its details changed",
    "DISAPPEARED": "was removed from the schedule",
    "NEWLY_BLOCKED": "was blocked by the warehouse",
    "NEWLY_HELD": "is now being held for another driver",
    "NEWLY_OCCUPIED": "was booked by someone else",
    "NEWLY_INCOMPATIBLE": "no longer suits this load",
    "NEWLY_UNREACHABLE": "can no longer be reached in time from the truck's current position",
    "NO_LONGER_FEASIBLE": "can no longer be used",
    "DATA_NOW_INCOMPLETE": "cannot be confirmed right now",
}


# One line of a stored menu, with the fingerprints needed to detect later change.
class OptionItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    rank: int
    slot_id: str
    # The slot's fingerprint when it was shown.
    slot_version: str
    # The facility rules' fingerprint when it was shown.
    rules_version: str
    # A hash of the decision at that moment.
    feasibility_hash: str
    # The full decision, kept so the exact offer can be reconstructed.
    decision: FeasibilityDecision


# A whole stored menu: who it was shown to, when, and the context it was computed in.
class OptionSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    option_set_id: str
    driver_id: str
    thread_id: str
    shipment_id: str
    facility_id: str
    shown_at: Aware
    # After this the menu can no longer be selected from.
    expires_at: Aware
    rules_version: str
    context: FeasibilityContext
    items: tuple[OptionItem, ...]


# How one option changed since it was shown.
class OptionDiffItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    rank: int
    slot_id: str
    change: Change
    reason_codes: tuple[str, ...] = ()
    # A short sentence the system or model can use.
    explanation: str
    # The slot's current verdict, if the slot still exists.
    current: FeasibilityDecision | None = None


# How the whole menu changed since it was shown.
class OptionDiff(BaseModel):
    model_config = ConfigDict(frozen=True)

    option_set_id: str
    evaluated_at: Aware
    # Past its 15-minute life.
    expired: bool
    # The facility's rules changed after the menu was built.
    rules_changed: bool
    items: tuple[OptionDiffItem, ...]

    @property
    def all_unchanged(self) -> bool:
        # True only if nothing at all has moved: not expired, same rules, every option unchanged.
        return not self.expired and not self.rules_changed and all(i.change == "UNCHANGED" for i in self.items)

    @property
    def still_offerable(self) -> tuple[int, ...]:
        # The ranks that can still be offered (unchanged, or changed in a harmless way).
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
    # The thread must belong to the driver in the context; otherwise someone is trying to attach options to another's chat.
    thread = conn.execute("select driver_id from chat_threads where thread_id=?", (thread_id,)).fetchone()
    if thread is None or thread["driver_id"] != ctx.driver_id:
        raise AuthError("FOREIGN_THREAD")
    # The shipment must also belong to that driver.
    owner = conn.execute("select driver_id from v_driver_shipments where shipment_id=?", (ctx.shipment_id,)).fetchone()
    if owner is None or owner["driver_id"] != ctx.driver_id:
        raise AuthError("FOREIGN_SHIPMENT")
    now = clock.now()
    # A readable unique id: "OPT-" plus 12 hex characters.
    option_set_id = "OPT-" + uuid.uuid4().hex[:12].upper()
    rules_version = state.rules_version()
    # "with conn" is one transaction: the header and all item rows are written together or not at all.
    with conn:
        conn.execute(
            "insert into option_sets (option_set_id, driver_id, thread_id, shipment_id, facility_id, "
            "shown_at, expires_at, rules_version, context_json) values (?,?,?,?,?,?,?,?,?)",
            (option_set_id, ctx.driver_id, thread_id, ctx.shipment_id, ctx.facility_id,
             now.isoformat(), (now + ttl).isoformat(), rules_version, ctx.model_dump_json()),
        )
        # One row per displayed option, keeping its rank, fingerprints and the full decision.
        for o in options:
            d = o.decision
            conn.execute(
                "insert into option_set_items (option_set_id, displayed_rank, slot_id, slot_version, "
                "rules_version, feasibility_hash, decision_json) values (?,?,?,?,?,?,?)",
                (option_set_id, o.rank, d.slot_id, d.slot_version, rules_version, d.digest, d.model_dump_json()),
            )
    # Read it back through the normal loader so the caller gets exactly what is stored.
    return _load(conn, option_set_id)


# ---- reading ---------------------------------------------------------------------------------
def _load(conn: sqlite3.Connection, option_set_id: str) -> OptionSet:
    # The header row and its items, in displayed order.
    head = conn.execute("select * from option_sets where option_set_id=?", (option_set_id,)).fetchone()
    rows = conn.execute(
        "select * from option_set_items where option_set_id=? order by displayed_rank", (option_set_id,)
    ).fetchall()
    # Rebuild the typed objects from the stored JSON and columns.
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
    # The set is returned only if driver, thread AND shipment all match. A set belonging to someone else
    # gives the same error as one that does not exist, so ids cannot be probed.
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
    # The newest menu shown to this driver in this thread for this shipment. rowid breaks ties if two share a timestamp.
    row = conn.execute(
        "select option_set_id from option_sets where driver_id=? and thread_id=? and shipment_id=? "
        "order by shown_at desc, rowid desc limit 1",
        (driver_id, thread_id, shipment_id),
    ).fetchone()
    return _load(conn, row["option_set_id"]) if row else None


# ---- reconciliation --------------------------------------------------------------------------
def _classify(item: OptionItem, cur: FeasibilityDecision) -> tuple[Change, tuple[str, ...]]:
    # Still feasible: unchanged if neither the slot fingerprint nor the decision hash moved, otherwise "changed but fine".
    if cur.status == "FEASIBLE":
        same = cur.slot_version == item.slot_version and cur.digest == item.feasibility_hash
        return ("UNCHANGED" if same else "CHANGED_STILL_FEASIBLE"), ()
    reasons = set(cur.reasons)
    codes = cur.reasons
    # Order matters: the most specific, driver-meaningful cause is reported first.
    if "SLOT_HELD" in reasons:
        return "NEWLY_HELD", codes
    if "SLOT_OCCUPIED" in reasons:
        return "NEWLY_OCCUPIED", codes
    if reasons & _BLOCKED:
        return "NEWLY_BLOCKED", codes
    if reasons & _INCOMPATIBLE:
        return "NEWLY_INCOMPATIBLE", codes
    # The truck cannot get there before the slot starts (its position changed, or time passed).
    if "RELEASE_AFTER_SLOT_START" in reasons:
        return "NEWLY_UNREACHABLE", codes
    if cur.status == "INCOMPLETE_FEASIBILITY_DATA":
        return "DATA_NOW_INCOMPLETE", codes
    # Anything else that makes it infeasible now.
    return "NO_LONGER_FEASIBLE", codes


def context_now(option_set: OptionSet, now: datetime, release_floor: datetime | None = None) -> FeasibilityContext:
    """The stored context at ``now``. If the truck cannot arrive before ``release_floor`` (from its latest GPS position),
    that later moment replaces the stored arrival time; an earlier floor never changes it."""
    ctx = option_set.context.with_now(now)
    # Only ever later: the driver's own figure is never made more optimistic by GPS.
    if release_floor is not None and release_floor > ctx.release_time:
        ctx = ctx.model_copy(update={"release_time": release_floor})
    return ctx


def diff_option_set(option_set: OptionSet, now: datetime, state: FacilityState, release_floor: datetime | None = None) -> OptionDiff:
    """Pure core: compare what was shown with ``state`` as of ``now`` (and, optionally, the truck's GPS arrival floor)."""
    # Re-use the original context but evaluated at the current moment (and the truck's current position).
    ctx = context_now(option_set, now, release_floor)
    items: list[OptionDiffItem] = []
    for it in option_set.items:
        slot = state.slots.get(it.slot_id)
        # The slot no longer exists in the schedule at all.
        if slot is None:
            items.append(OptionDiffItem(rank=it.rank, slot_id=it.slot_id, change="DISAPPEARED",
                                        explanation=f"Option {it.rank} {_TEXT['DISAPPEARED']}."))
            continue
        # Re-judge the slot with the same rules, then classify the difference.
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
    # A fresh snapshot of the facility right now.
    state = build_facility_state(conn, option_set.facility_id, now)
    # Where the truck is now: options it can no longer reach in time are reported as such.
    floor = gps_arrival_floor(conn, option_set.shipment_id, clock, load_settings())
    return diff_option_set(option_set, now, state, floor)


# ---- retention -------------------------------------------------------------------------------
def purge_option_sets(conn: sqlite3.Connection, before: datetime) -> int:
    """Delete sets shown before ``before``. Compared as instants, never as strings."""
    # Compare parsed instants, not text, because timestamps may carry different offsets and text order would be wrong.
    stale = [
        r["option_set_id"]
        for r in conn.execute("select option_set_id, shown_at from option_sets")
        if parse_operational_timestamp(r["shown_at"]) < before
    ]
    # One transaction for all deletes; the items are removed with their set (cascade).
    with conn:
        for option_set_id in stale:
            conn.execute("delete from option_sets where option_set_id=?", (option_set_id,))
    return len(stale)
