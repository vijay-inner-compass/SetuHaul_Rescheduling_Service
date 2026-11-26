"""Holds inside the appointment capacity boundary.

A hold is an ``appointments`` row with status HELD, covered by the same active-slot unique index
as every other active appointment, so a planner, a scheduling tool or another driver collides with
it on the database constraint rather than on application code. ``appointment_hold_meta`` only
extends the row (owner, option set, expiry); it is not the lock.

Model calls never happen inside these transactions.
"""

# Lazy annotations.
from __future__ import annotations

# hashlib/json build request fingerprints.
import hashlib
import json

# sqlite3 for the Connection type and its error classes.
import sqlite3

# uuid makes unique appointment ids.
import uuid

# Callable types the test fault hook.
from collections.abc import Callable

# datetime/timedelta for times and hold lifetimes.
from datetime import datetime, timedelta

# StrEnum for the outcome classes.
from enum import StrEnum

# Literal restricts status strings.
from typing import Literal

# Frozen validated models.
from pydantic import BaseModel, ConfigDict

# The pass window follows a confirmed reschedule.
from setuhaul import passes

# Facility snapshot loader.
from setuhaul.capacity import build_facility_state

# The injectable clock.
from setuhaul.clock import Clock

# Settings give the hold lifetime.
from setuhaul.config import load_settings

# immediate(): the write transaction; parse_operational_timestamp for stored times.
from setuhaul.db import immediate, parse_operational_timestamp

# History and outbox writers.
from setuhaul.events import append_appointment_event, enqueue_notification

# Slot re-evaluation.
from setuhaul.feasibility import evaluate_slot

# The truck's GPS arrival floor.
from setuhaul.gps import gps_arrival_floor

# Authorisation error.
from setuhaul.identity import AuthError

# Downstream notices for confirmed changes.
from setuhaul.notifications import enqueue_downstream

# Stored menus and their comparison with live state.
from setuhaul.option_history import OptionDiffItem, context_now, diff_option_set, get_option_set

# The text queued to the driver when a hold lapses.
HOLD_EXPIRED_MESSAGE = (
    "Your hold on that slot has lapsed. No booking has changed, and your original appointment "
    "still stands. I can search for fresh options if you would like."
)


# How an operation ended, in terms an API or a monitor can act on.
class OutcomeClass(StrEnum):
    # It worked.
    SUCCESS = "SUCCESS"
    # A normal "no": slot taken, hold expired, not allowed. The system is healthy.
    BUSINESS_CONFLICT = "BUSINESS_CONFLICT"
    # The database was busy; retrying later may work. Nothing was changed.
    LOCK_TIMEOUT = "LOCK_TIMEOUT"
    # Something unexpected failed; everything was rolled back.
    SYSTEM_ERROR = "SYSTEM_ERROR"


# Every way a hold request can end.
HoldStatus = Literal[
    "HELD", "SLOT_CONFLICT", "SHIPMENT_ALREADY_HELD", "OPTION_CHANGED", "OPTION_EXPIRED",
    "INVALID_OPTION", "LOCK_TIMEOUT", "SYSTEM_ERROR",
]


class HoldResult(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    status: HoldStatus
    outcome: OutcomeClass
    appointment_id: str | None = None
    slot_id: str | None = None
    # When the hold lapses, if one was placed.
    expires_at: datetime | None = None
    # The shipment's other existing hold, when this request clashes with it.
    existing_appointment_id: str | None = None
    # What changed about the option since it was shown, when that is why it failed.
    diff: OptionDiffItem | None = None


# A read-only view of one hold and its state right now.
class HoldView(BaseModel):
    model_config = ConfigDict(frozen=True)

    appointment_id: str
    shipment_id: str
    slot_id: str
    driver_id: str
    thread_id: str
    option_set_id: str
    expires_at: datetime
    state: Literal["LIVE", "EXPIRED", "RELEASED", "CONFIRMED"]


class ReleaseResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["RELEASED", "ALREADY_RELEASED", "NOT_HELD"]
    outcome: OutcomeClass = OutcomeClass.SUCCESS


def _fail(status: HoldStatus, outcome: OutcomeClass = OutcomeClass.BUSINESS_CONFLICT, **kw: object) -> HoldResult:
    # Shorthand for a failed HoldResult; most failures are ordinary business conflicts.
    return HoldResult(status=status, outcome=outcome, **kw)  # type: ignore[arg-type]


def _is_lock_error(exc: sqlite3.OperationalError) -> bool:
    # SQLite reports contention as "database is locked" or "busy"; these are retryable, unlike other operational errors.
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _fingerprint(**parts: str) -> str:
    # A stable hash of the named parts. Used to tell "the same request again" from "a different request reusing an id".
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


# ---- expiry (shared by lazy expiry here and the sweep job) -------------------------------------
def expire_hold_in_txn(conn: sqlite3.Connection, appointment_id: str, now: datetime) -> bool:
    """HELD -> CANCELLED, capacity released, history appended, driver notice queued.

    Caller owns the transaction. Returns False if the row is no longer HELD (idempotent).
    """
    # Only a row that is still HELD qualifies; anything else (confirmed, released, already expired) is left alone.
    row = conn.execute(
        "select a.shipment_id, m.driver_id, m.thread_id, m.expires_at from appointments a "
        "join appointment_hold_meta m using(appointment_id) "
        "where a.appointment_id=? and a.appointment_status='HELD'",
        (appointment_id,),
    ).fetchone()
    if row is None:
        return False
    stamp = now.isoformat()
    # Cancelling the row frees the slot: the unique index only covers active statuses.
    # The "and appointment_status='HELD'" guard repeats the check inside the UPDATE, so two racing expiries cannot both win.
    conn.execute(
        "update appointments set appointment_status='CANCELLED', cancelled_at=?, "
        "cancellation_reason='HOLD_EXPIRED', updated_at=? where appointment_id=? and appointment_status='HELD'",
        (stamp, stamp, appointment_id),
    )
    # The metadata records that the hold ended and why.
    conn.execute(
        "update appointment_hold_meta set released_at=?, release_reason='EXPIRED' where appointment_id=?",
        (stamp, appointment_id),
    )
    # History: the system ended the hold because its time ran out.
    event_id = append_appointment_event(
        conn, appointment_id, row["shipment_id"], "HOLD_EXPIRED", from_status="HELD", to_status="CANCELLED",
        actor_type="SYSTEM", actor_id=None, reason="HOLD_TTL_ELAPSED", occurred_at=now,
    )
    # Queue a notice for the driver; the dedupe key means it is queued once even if expiry runs twice.
    enqueue_notification(
        conn, f"HOLD_EXPIRED:{appointment_id}", "DRIVER", row["driver_id"], event_id,
        {"kind": "HOLD_EXPIRED", "thread_id": row["thread_id"], "appointment_id": appointment_id,
         "text": HOLD_EXPIRED_MESSAGE},
        # Dated when the hold ran out, not when it was noticed: after downtime the sender skips it as old news.
        min(now, parse_operational_timestamp(row["expires_at"])),
    )
    return True


def _expire_due_for(conn: sqlite3.Connection, now: datetime, *, shipment_id: str, slot_id: str) -> None:
    # Lazy expiry: before placing a hold, clear any overdue hold on the same shipment or the same slot,
    # so a lapsed hold never blocks a new one even if the sweep job has not run yet.
    rows = conn.execute(
        "select a.appointment_id from appointments a join appointment_hold_meta m using(appointment_id) "
        "where a.appointment_status='HELD' and m.expires_epoch <= ? and (a.shipment_id=? or a.slot_id=?)",
        (int(now.timestamp()), shipment_id, slot_id),
    ).fetchall()
    for r in rows:
        expire_hold_in_txn(conn, r["appointment_id"], now)


# ---- acquire -----------------------------------------------------------------------------------
def acquire_hold(
    conn: sqlite3.Connection,
    driver_id: str,
    thread_id: str,
    shipment_id: str,
    option_set_id: str,
    rank: int,
    clock: Clock,
    ttl_seconds: int | None = None,
    supersede: bool = False,
) -> HoldResult:
    """Hold the slot shown at ``rank``. First successful commit wins (FCFS, decision D5)."""
    # Load the menu the driver saw. This also proves the menu belongs to this driver, thread and shipment.
    opt = get_option_set(conn, driver_id, thread_id, shipment_id, option_set_id)  # raises AuthError
    # The chosen line; a rank that was never shown is invalid.
    item = next((i for i in opt.items if i.rank == rank), None)
    if item is None:
        return _fail("INVALID_OPTION")
    # Hold lifetime: the caller's value (tests) or the configured 180 seconds.
    ttl = timedelta(seconds=ttl_seconds if ttl_seconds is not None else load_settings().hold_ttl_seconds)
    try:
        # BEGIN IMMEDIATE: take the write lock now. Concurrent requests queue behind it.
        with immediate(conn):
            now = clock.now()
            # Clear overdue holds that could be in the way.
            _expire_due_for(conn, now, shipment_id=shipment_id, slot_id=item.slot_id)
            # The menu itself has a 15-minute life.
            if now >= opt.expires_at:
                return _fail("OPTION_EXPIRED")
            # Does this shipment already hold a slot?
            existing = conn.execute(
                "select appointment_id, slot_id from appointments where shipment_id=? and appointment_status='HELD'",
                (shipment_id,),
            ).fetchone()
            if existing is not None:
                # The same slot again: idempotent, return the existing hold (a driver tapping "take option 1" twice).
                if existing["slot_id"] == item.slot_id:
                    meta = conn.execute("select expires_at from appointment_hold_meta where appointment_id=?",
                                        (existing["appointment_id"],)).fetchone()
                    return HoldResult(
                        status="HELD", outcome=OutcomeClass.SUCCESS, appointment_id=existing["appointment_id"],
                        slot_id=item.slot_id, expires_at=parse_operational_timestamp(meta["expires_at"]),
                    )
                # A different slot: one hold per shipment. Refuse and say which hold exists, unless the driver is deliberately
                # switching to another option (supersede): then the old hold is released below, only once the new one is certain.
                if not supersede:
                    return _fail("SHIPMENT_ALREADY_HELD", existing_appointment_id=existing["appointment_id"])
            # Re-check the chosen option against the facility as it is right now, inside the lock.
            state = build_facility_state(conn, opt.facility_id, now)
            # Where the truck is now (latest fresh GPS ping): a slot it can no longer reach in time is refused.
            floor = gps_arrival_floor(conn, shipment_id, clock, load_settings())
            diff_item = next(i for i in diff_option_set(opt, now, state, floor).items if i.rank == rank)
            # Anything other than "still available" fails. A slot taken by someone else is a SLOT_CONFLICT;
            # any other change (blocked, no longer suitable) is OPTION_CHANGED.
            if diff_item.change not in ("UNCHANGED", "CHANGED_STILL_FEASIBLE"):
                taken = diff_item.change in ("NEWLY_HELD", "NEWLY_OCCUPIED")
                return _fail("SLOT_CONFLICT" if taken else "OPTION_CHANGED", diff=diff_item)
            # Switching holds: the new option has passed every check, so release the old hold in the same transaction.
            # If the insert below fails, the whole transaction rolls back and the old hold is kept.
            if existing is not None and existing["slot_id"] != item.slot_id:
                release_hold_in_txn(conn, existing["appointment_id"], shipment_id, "SUPERSEDED", now, "DRIVER", driver_id,
                                    "SUPERSEDED_BY_NEW_CHOICE")
            # The shipment's current appointment, which the hold would eventually replace.
            original = conn.execute(
                "select appointment_id from appointments where shipment_id=? and is_current=1 "
                "and appointment_status in ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')",
                (shipment_id,),
            ).fetchone()
            appointment_id = "APT-H-" + uuid.uuid4().hex[:10].upper()
            expires = now + ttl
            stamp = now.isoformat()
            # The hold is an appointments row (status HELD, not current). Because it is in the same table and
            # the same unique index as real bookings, the database itself rejects a second claim on the slot.
            conn.execute(
                "insert into appointments (appointment_id, shipment_id, slot_id, appointment_status, "
                "booking_source, is_current, booked_at, replaced_appointment_id, updated_at) "
                "values (?,?,?, 'HELD','DRIVER_CHAT',0,?,?,?)",
                (appointment_id, shipment_id, item.slot_id, stamp,
                 original["appointment_id"] if original else None, stamp),
            )
            # Hold-specific details: owner, the menu it came from, expiry (as text and as an integer for fast comparison),
            # and a fingerprint of the request.
            conn.execute(
                "insert into appointment_hold_meta (appointment_id, driver_id, thread_id, option_set_id, "
                "expires_at, expires_epoch, request_fingerprint) values (?,?,?,?,?,?,?)",
                (appointment_id, driver_id, thread_id, option_set_id, expires.isoformat(),
                 int(expires.timestamp()),
                 _fingerprint(driver=driver_id, thread=thread_id, shipment=shipment_id,
                              option_set=option_set_id, slot=item.slot_id, slot_version=item.slot_version)),
            )
            # History: the driver picked this option.
            append_appointment_event(
                conn, appointment_id, shipment_id, "HOLD_ACQUIRED", from_status=None, to_status="HELD",
                actor_type="DRIVER", actor_id=driver_id, reason="DRIVER_SELECTED_OPTION", occurred_at=now,
                details={"slot_id": item.slot_id, "option_set_id": option_set_id, "rank": rank,
                         "expires_at": expires.isoformat()},
            )
            # Returning inside "with immediate" commits everything above in one step.
            return HoldResult(status="HELD", outcome=OutcomeClass.SUCCESS, appointment_id=appointment_id,
                              slot_id=item.slot_id, expires_at=expires)
    except sqlite3.IntegrityError:
        # The shared unique index fired: someone else's row won the slot (or the shipment's hold).
        return _fail("SLOT_CONFLICT")
    except sqlite3.OperationalError as exc:
        # A busy database is retryable; any other operational error is a genuine system problem.
        if _is_lock_error(exc):
            return _fail("LOCK_TIMEOUT", OutcomeClass.LOCK_TIMEOUT)
        return _fail("SYSTEM_ERROR", OutcomeClass.SYSTEM_ERROR)


# ---- release and inspection --------------------------------------------------------------------
def release_hold(
    conn: sqlite3.Connection, driver_id: str, thread_id: str, appointment_id: str, reason: str, clock: Clock
) -> ReleaseResult:
    # Only these three reasons are valid; anything else is a programming error, not user input.
    if reason not in ("DRIVER_CANCELLED", "SUPERSEDED", "SYSTEM"):
        raise ValueError(f"unsupported release reason {reason!r}")
    with immediate(conn):
        # The hold and its metadata.
        row = conn.execute(
            "select a.shipment_id, a.appointment_status, m.driver_id, m.thread_id, m.released_at "
            "from appointments a join appointment_hold_meta m using(appointment_id) where a.appointment_id=?",
            (appointment_id,),
        ).fetchone()
        # Not found or not this driver's hold in this thread: same error for both.
        if row is None or row["driver_id"] != driver_id or row["thread_id"] != thread_id:
            raise AuthError("FOREIGN_HOLD")
        # Not HELD any more: report whether it was released earlier or was never an active hold.
        if row["appointment_status"] != "HELD":
            return ReleaseResult(status="ALREADY_RELEASED" if row["released_at"] else "NOT_HELD")
        now = clock.now()
        stamp = now.isoformat()
        # Cancel the hold row (freeing the slot) and stamp the metadata.
        conn.execute(
            "update appointments set appointment_status='CANCELLED', cancelled_at=?, "
            "cancellation_reason='HOLD_RELEASED', updated_at=? where appointment_id=?",
            (stamp, stamp, appointment_id),
        )
        conn.execute("update appointment_hold_meta set released_at=?, release_reason=? where appointment_id=?",
                     (stamp, reason, appointment_id))
        # History records whether the driver or the system released it.
        append_appointment_event(
            conn, appointment_id, row["shipment_id"], "HOLD_RELEASED", from_status="HELD", to_status="CANCELLED",
            actor_type="DRIVER" if reason == "DRIVER_CANCELLED" else "SYSTEM",
            actor_id=driver_id if reason == "DRIVER_CANCELLED" else None, reason=reason, occurred_at=now,
        )
    return ReleaseResult(status="RELEASED")


def get_hold(conn: sqlite3.Connection, appointment_id: str, clock: Clock) -> HoldView | None:
    # The hold row joined with its metadata.
    r = conn.execute(
        "select a.appointment_id, a.shipment_id, a.slot_id, a.appointment_status, m.driver_id, m.thread_id, "
        "m.option_set_id, m.expires_at, m.expires_epoch, m.release_reason "
        "from appointments a join appointment_hold_meta m using(appointment_id) where a.appointment_id=?",
        (appointment_id,),
    ).fetchone()
    if r is None:
        return None
    # Work out the state. A row still marked HELD may already be past its expiry (the sweep has not run yet),
    # so compare the expiry time with now rather than trusting the status alone.
    if r["appointment_status"] == "HELD":
        state: Literal["LIVE", "EXPIRED", "RELEASED", "CONFIRMED"] = (
            "EXPIRED" if r["expires_epoch"] <= int(clock.now().timestamp()) else "LIVE"
        )
    elif r["release_reason"] == "EXPIRED":
        state = "EXPIRED"
    elif r["release_reason"] == "CONFIRMED" or r["appointment_status"] == "CONFIRMED":
        state = "CONFIRMED"
    else:
        state = "RELEASED"
    return HoldView(
        appointment_id=r["appointment_id"], shipment_id=r["shipment_id"], slot_id=r["slot_id"],
        driver_id=r["driver_id"], thread_id=r["thread_id"], option_set_id=r["option_set_id"],
        expires_at=parse_operational_timestamp(r["expires_at"]), state=state,
    )


def make_hold_checker(conn: sqlite3.Connection, clock: Clock):  # type: ignore[no-untyped-def]
    """Adapter for ``ConversationMemoryStore(hold_checker=...)``: live hold state, not a stale promise."""

    def check(appointment_id: str) -> Literal["LIVE", "EXPIRED", "GONE"]:
        view = get_hold(conn, appointment_id, clock)
        # No such hold at all.
        if view is None:
            return "GONE"
        if view.state == "LIVE":
            return "LIVE"
        # Expired holds are distinguished from released or confirmed ones, because the driver should be told about expiry.
        return "EXPIRED" if view.state == "EXPIRED" else "GONE"

    return check


# ---- shared in-transaction release ----------------------------------------------------------------
def release_hold_in_txn(
    conn: sqlite3.Connection, appointment_id: str, shipment_id: str, reason: str, now: datetime,
    actor_type: str, actor_id: str | None, event_reason: str,
) -> None:
    """HELD -> CANCELLED. Caller owns the transaction and has already checked ownership."""
    stamp = now.isoformat()
    # The "and appointment_status='HELD'" guard makes the release a no-op if the row has already moved on.
    conn.execute(
        "update appointments set appointment_status='CANCELLED', cancelled_at=?, "
        "cancellation_reason='HOLD_RELEASED', updated_at=? where appointment_id=? and appointment_status='HELD'",
        (stamp, stamp, appointment_id),
    )
    conn.execute("update appointment_hold_meta set released_at=?, release_reason=? where appointment_id=?",
                 (stamp, reason, appointment_id))
    append_appointment_event(
        conn, appointment_id, shipment_id, "HOLD_RELEASED", from_status="HELD", to_status="CANCELLED",
        actor_type=actor_type, actor_id=actor_id, reason=event_reason, occurred_at=now,
    )


# ---- atomic confirmation ---------------------------------------------------------------------------
# The idempotency "action" name for confirmations.
ACTION_CONFIRM = "CONFIRM_RESCHEDULE"
fault_hook: Callable[[str], None] | None = None  # tests inject failures at named write points


def _fault(point: str) -> None:
    # In production fault_hook is None and this does nothing. Tests set it to raise at a named step,
    # to prove that a failure part-way through confirmation rolls EVERYTHING back.
    if fault_hook is not None:
        fault_hook(point)


# The facts that define one confirmation request.
class ConfirmCommand(BaseModel):
    model_config = ConfigDict(frozen=True)

    driver_id: str
    thread_id: str
    # The inbound message id: together with the action it makes the request idempotent.
    message_id: str
    shipment_id: str
    hold_appointment_id: str

    def fingerprint(self) -> str:
        # What makes two requests "the same": shipment, hold and action (not the message id, which is the lookup key).
        return _fingerprint(shipment=self.shipment_id, hold=self.hold_appointment_id, action=ACTION_CONFIRM)


# Every way a confirmation can end.
ConfirmStatus = Literal[
    "CONFIRMED", "ALREADY_CONFIRMED", "IDEMPOTENCY_MISMATCH", "HOLD_EXPIRED", "HOLD_NOT_ACTIVE",
    "FEASIBILITY_CHANGED", "APPROVAL_REQUIRED", "ORIGINAL_NOT_RESCHEDULABLE", "LOCK_TIMEOUT", "SYSTEM_ERROR",
]


class ConfirmationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: ConfirmStatus
    outcome: OutcomeClass
    appointment_id: str | None = None
    slot_id: str | None = None
    # The appointment this confirmation replaced.
    original_appointment_id: str | None = None
    reason_codes: tuple[str, ...] = ()
    # True when this is the stored answer to a repeated message.
    replayed: bool = False


def _result(status: ConfirmStatus, **kw: object) -> ConfirmationResult:
    # CONFIRMED and ALREADY_CONFIRMED are successes; every other expected answer is a business conflict.
    outcome = OutcomeClass.SUCCESS if status in ("CONFIRMED", "ALREADY_CONFIRMED") else OutcomeClass.BUSINESS_CONFLICT
    return ConfirmationResult(status=status, outcome=outcome, **kw)  # type: ignore[arg-type]


def get_idempotent_result(
    conn: sqlite3.Connection, driver_id: str, thread_id: str, message_id: str, action: str, fingerprint: str
) -> tuple[Literal["MISSING", "REPLAY", "MISMATCH"], ConfirmationResult | None]:
    # Look for an earlier answer to this exact (driver, thread, message, action).
    row = conn.execute(
        "select fingerprint, result_json from idempotency_keys where driver_id=? and thread_id=? "
        "and message_id=? and action=?", (driver_id, thread_id, message_id, action),
    ).fetchone()
    # Never seen: process normally.
    if row is None:
        return "MISSING", None
    # Seen, but for a different request: the id is being reused wrongly.
    if row["fingerprint"] != fingerprint:
        return "MISMATCH", None
    # Seen and identical: replay the stored answer instead of acting again.
    return "REPLAY", ConfirmationResult.model_validate_json(row["result_json"])


def _store_result(conn: sqlite3.Connection, cmd: ConfirmCommand, result: ConfirmationResult, now: datetime) -> ConfirmationResult:
    # Saved inside the same transaction as the work, so the answer exists if and only if the work committed.
    conn.execute(
        "insert into idempotency_keys (driver_id, thread_id, message_id, action, fingerprint, result_json, "
        "created_at) values (?,?,?,?,?,?,?)",
        (cmd.driver_id, cmd.thread_id, cmd.message_id, ACTION_CONFIRM, cmd.fingerprint(),
         result.model_dump_json(), now.isoformat()),
    )
    return result


def confirm_reschedule(conn: sqlite3.Connection, cmd: ConfirmCommand, clock: Clock) -> ConfirmationResult:
    """Promote a live hold to CONFIRMED and end the original appointment in one transaction.

    The original stays active until this transaction commits. Any failure rolls everything back and
    is reported as SYSTEM_ERROR / LOCK_TIMEOUT; nothing is promised to the driver in that case.
    """
    try:
        # One write transaction for the whole confirmation: all steps below commit together or not at all.
        with immediate(conn):
            now = clock.now()
            # Idempotency: has this message already been handled?
            state_, stored = get_idempotent_result(
                conn, cmd.driver_id, cmd.thread_id, cmd.message_id, ACTION_CONFIRM, cmd.fingerprint())
            if state_ == "MISMATCH":
                return _result("IDEMPOTENCY_MISMATCH")
            if state_ == "REPLAY" and stored is not None:
                return stored.model_copy(update={"replayed": True})

            # The hold, with its owner and expiry.
            row = conn.execute(
                "select a.appointment_id, a.shipment_id, a.slot_id, a.appointment_status, m.driver_id, m.thread_id, "
                "m.option_set_id, m.expires_epoch, m.release_reason from appointments a "
                "join appointment_hold_meta m using(appointment_id) where a.appointment_id=?",
                (cmd.hold_appointment_id,),
            ).fetchone()
            # The hold must exist and belong to this driver, thread and shipment.
            if (row is None or row["driver_id"] != cmd.driver_id or row["thread_id"] != cmd.thread_id
                    or row["shipment_id"] != cmd.shipment_id):
                raise AuthError("FOREIGN_HOLD")
            # Already confirmed (a second "yes"): succeed again without doing anything.
            if row["appointment_status"] == "CONFIRMED":
                return _store_result(conn, cmd, _result(
                    "ALREADY_CONFIRMED", appointment_id=row["appointment_id"], slot_id=row["slot_id"]), now)
            # Not HELD any more (expired earlier, or released): explain which.
            if row["appointment_status"] != "HELD":
                status: ConfirmStatus = "HOLD_EXPIRED" if row["release_reason"] == "EXPIRED" else "HOLD_NOT_ACTIVE"
                return _store_result(conn, cmd, _result(status), now)
            # Past its expiry even though still marked HELD: expire it now and report HOLD_EXPIRED.
            if row["expires_epoch"] <= int(now.timestamp()):
                expire_hold_in_txn(conn, cmd.hold_appointment_id, now)
                return _store_result(conn, cmd, _result("HOLD_EXPIRED"), now)

            # Re-judge the held slot against the facility as it is right now, inside the lock.
            opt = get_option_set(conn, cmd.driver_id, cmd.thread_id, cmd.shipment_id, row["option_set_id"])
            # The load was moved to another facility after this slot was held: booking it would send the truck to the wrong site.
            bound_for = conn.execute("select destination_facility_id from shipments where shipment_id=?", (cmd.shipment_id,)).fetchone()[0]
            if bound_for != opt.facility_id:
                release_hold_in_txn(conn, cmd.hold_appointment_id, cmd.shipment_id, "SYSTEM", now, "SYSTEM", None, "DESTINATION_CHANGED")
                return _store_result(conn, cmd, _result("FEASIBILITY_CHANGED", reason_codes=("DESTINATION_CHANGED",)), now)
            state = build_facility_state(conn, opt.facility_id, now)
            # Judged from where the truck is now: if it moved so far away that it cannot reach the slot, the hold is released.
            floor = gps_arrival_floor(conn, cmd.shipment_id, clock, load_settings())
            decision = evaluate_slot(context_now(opt, now, floor), state.slots[row["slot_id"]], state)
            # No longer feasible (blocked, rules changed, needs approval ...): release the hold, keep the original.
            if decision.status != "FEASIBLE":
                release_hold_in_txn(conn, cmd.hold_appointment_id, cmd.shipment_id, "SYSTEM", now,
                                    "SYSTEM", None, "FEASIBILITY_LOST")
                kind: ConfirmStatus = "APPROVAL_REQUIRED" if decision.status == "REQUIRES_APPROVAL" else "FEASIBILITY_CHANGED"
                return _store_result(conn, cmd, _result(kind, reason_codes=decision.reasons or decision.missing), now)

            # The shipment's current appointment, which this confirmation will replace.
            orig = conn.execute(
                "select appointment_id, appointment_status, slot_id from appointments where shipment_id=? "
                "and is_current=1 and appointment_status in ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')",
                (cmd.shipment_id,),
            ).fetchone()
            # If unloading has already started, the appointment cannot be moved: release the hold and say so.
            if orig is not None and orig["appointment_status"] == "IN_PROGRESS":
                release_hold_in_txn(conn, cmd.hold_appointment_id, cmd.shipment_id, "SYSTEM", now,
                                    "SYSTEM", None, "ORIGINAL_IN_PROGRESS")
                return _store_result(conn, cmd, _result("ORIGINAL_NOT_RESCHEDULABLE"), now)

            stamp = now.isoformat()
            # Each _fault() is a named point where tests can force a crash to prove the rollback is complete.
            _fault("BEFORE_END_ORIGINAL")
            # Step 1: end the original appointment (only now, inside the transaction).
            if orig is not None:
                conn.execute(
                    "update appointments set appointment_status='CANCELLED', is_current=0, cancelled_at=?, "
                    "cancellation_reason='RESCHEDULED', updated_at=? where appointment_id=?",
                    (stamp, stamp, orig["appointment_id"]),
                )
                append_appointment_event(
                    conn, orig["appointment_id"], cmd.shipment_id, "ORIGINAL_ENDED",
                    from_status=orig["appointment_status"], to_status="CANCELLED", actor_type="DRIVER",
                    actor_id=cmd.driver_id, reason="RESCHEDULED", occurred_at=now,
                    details={"replaced_by": cmd.hold_appointment_id},
                )
            _fault("AFTER_END_ORIGINAL")
            # Step 2: promote the hold to a CONFIRMED, current appointment, linked to the one it replaced.
            conn.execute(
                "update appointments set appointment_status='CONFIRMED', is_current=1, confirmed_at=?, "
                "replaced_appointment_id=?, updated_at=? where appointment_id=? and appointment_status='HELD'",
                (stamp, orig["appointment_id"] if orig else None, stamp, cmd.hold_appointment_id),
            )
            _fault("AFTER_PROMOTE")
            # Step 3: the hold's metadata records that it ended by being confirmed.
            conn.execute("update appointment_hold_meta set released_at=?, release_reason='CONFIRMED' where appointment_id=?",
                         (stamp, cmd.hold_appointment_id))
            _fault("AFTER_META")
            # Step 4: history entry describing the move from the old slot to the new one.
            details = {"from_slot_id": orig["slot_id"] if orig else None, "to_slot_id": row["slot_id"],
                       "driver_id": cmd.driver_id}
            event_id = append_appointment_event(
                conn, cmd.hold_appointment_id, cmd.shipment_id, "RESCHEDULE_CONFIRMED", from_status="HELD",
                to_status="CONFIRMED", actor_type="DRIVER", actor_id=cmd.driver_id,
                reason="DRIVER_CONFIRMED_OPTION", occurred_at=now, details=details,
            )
            _fault("AFTER_EVENTS")
            # Step 5: notices to the planner, consignee and operations (delivered later, outside the transaction).
            enqueue_downstream(conn, event_id, "RESCHEDULE_CONFIRMED", cmd.shipment_id, cmd.hold_appointment_id,
                               {**details, "from_time": None, "to_time": decision.slot_start.isoformat()}, now)
            _fault("AFTER_OUTBOX")
            # The Dock Access Pass keeps its code; its window moves to the new slot in the same transaction.
            passes.sync_window(conn, cmd.shipment_id, clock)
            # Step 6: record the answer for idempotency, then return it. Leaving the "with" block commits all six steps.
            result = _result("CONFIRMED", appointment_id=cmd.hold_appointment_id, slot_id=row["slot_id"],
                             original_appointment_id=orig["appointment_id"] if orig else None)
            _store_result(conn, cmd, result, now)
            _fault("AFTER_IDEMPOTENCY")
            return result
    # Authorisation failures propagate: they are not system errors.
    except AuthError:
        raise
    # A busy database is retryable and nothing was changed.
    except sqlite3.OperationalError as exc:
        if _is_lock_error(exc):
            return ConfirmationResult(status="LOCK_TIMEOUT", outcome=OutcomeClass.LOCK_TIMEOUT)
        return ConfirmationResult(status="SYSTEM_ERROR", outcome=OutcomeClass.SYSTEM_ERROR)
    # Anything else: the transaction rolled back (immediate() guarantees it), so report an error and promise nothing.
    except Exception:  # noqa: BLE001 - any failure must roll back and be reported as SYSTEM_ERROR, never raised to the driver turn
        return ConfirmationResult(status="SYSTEM_ERROR", outcome=OutcomeClass.SYSTEM_ERROR)


def confirmed_appointment_facts(conn: sqlite3.Connection, appointment_id: str) -> dict[str, str]:
    """Facts of a committed CONFIRMED appointment. Raises ValueError for anything else."""
    # Read the facts back from the database, so wording shown to the driver comes from committed state only.
    r = conn.execute(
        "select a.appointment_id, a.appointment_status, s.slot_start_ts, s.slot_end_ts, d.dock_code, f.facility_name "
        "from appointments a join appointment_slots s using(slot_id) join docks d on d.dock_id = s.dock_id "
        "join facilities f on f.facility_id = d.facility_id "
        "where a.appointment_id=?", (appointment_id,),
    ).fetchone()
    if r is None:
        raise ValueError(f"no such appointment {appointment_id}")
    # Refuse to describe anything that is not actually confirmed, so a "confirmation" message can never be wrong.
    if r["appointment_status"] != "CONFIRMED":
        raise ValueError(f"appointment {appointment_id} is not confirmed ({r['appointment_status']})")
    start = parse_operational_timestamp(r["slot_start_ts"])
    end = parse_operational_timestamp(r["slot_end_ts"])
    # Times as "HH:MM", date as "04 Aug 2026", plus the dock and the appointment reference.
    return {"dock": r["dock_code"], "start": f"{start:%H:%M}", "end": f"{end:%H:%M}",
            "date": f"{start:%d %b %Y}", "ref": r["appointment_id"], "facility": r["facility_name"]}


def render_confirmation_text(conn: sqlite3.Connection, appointment_id: str) -> str:
    """English wording for a confirmed booking, built only from the committed row."""
    f = confirmed_appointment_facts(conn, appointment_id)
    return (f"Your appointment is confirmed: dock {f['dock']}, {f['start']}-{f['end']} on "
            f"{f['date']}. Reference {f['ref']}.")


# ---- the truck moved ----------------------------------------------------------------------------------------
def reassess_hold_after_position_change(conn: sqlite3.Connection, shipment_id: str, clock: Clock) -> dict[str, str] | None:
    """A new GPS ping has arrived. If the shipment's live hold is for a slot the truck can no longer reach in time from
    where it is now, release the hold at once (the dock is freed for others).

    Returns the released slot's facts (appointment_id, start, end, dock) so the caller can tell the driver, or None when there is
    no hold, no fresh ping, or the slot is still reachable. The original appointment is never touched.
    """
    now = clock.now()
    # Where the truck is now; without a fresh ping there is nothing to reassess.
    floor = gps_arrival_floor(conn, shipment_id, clock, load_settings())
    if floor is None:
        return None
    with immediate(conn):
        # The shipment's live hold, if any (an expired one is the sweep's job).
        row = conn.execute(
            "select a.appointment_id, a.slot_id, m.driver_id, m.thread_id, m.option_set_id, m.expires_epoch "
            "from appointments a join appointment_hold_meta m using(appointment_id) "
            "where a.shipment_id=? and a.appointment_status='HELD'", (shipment_id,)).fetchone()
        if row is None or row["expires_epoch"] <= int(now.timestamp()):
            return None
        # Re-judge the held slot with the truck's new position.
        opt = get_option_set(conn, row["driver_id"], row["thread_id"], shipment_id, row["option_set_id"])
        state = build_facility_state(conn, opt.facility_id, now)
        slot = state.slots.get(row["slot_id"])
        if slot is None:
            return None
        decision = evaluate_slot(context_now(opt, now, floor), slot, state)
        # Only one reason matters here: the truck cannot arrive before the slot starts.
        if "RELEASE_AFTER_SLOT_START" not in decision.reasons:
            return None
        # Free the slot, recording that the system did it because the position changed.
        release_hold_in_txn(conn, row["appointment_id"], shipment_id, "SYSTEM", now, "SYSTEM", None, "POSITION_CHANGED")
    # What was released, for the message to the driver (written by the caller, which has the conversation memory).
    return {"appointment_id": str(row["appointment_id"]), "start": f"{slot.start:%H:%M}", "end": f"{slot.end:%H:%M}",
            "dock": state.docks[slot.dock_id].code}
