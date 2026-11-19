"""Holds inside the appointment capacity boundary.

A hold is an ``appointments`` row with status HELD, covered by the same active-slot unique index
as every other active appointment, so a planner, a scheduling tool or another driver collides with
it on the database constraint rather than on application code. ``appointment_hold_meta`` only
extends the row (owner, option set, expiry); it is not the lock.

Model calls never happen inside these transactions.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from setuhaul.capacity import build_facility_state
from setuhaul.clock import Clock
from setuhaul.config import load_settings
from setuhaul.db import immediate, parse_operational_timestamp
from setuhaul.events import append_appointment_event, enqueue_notification
from setuhaul.feasibility import evaluate_slot
from setuhaul.identity import AuthError
from setuhaul.notifications import enqueue_downstream
from setuhaul.option_history import OptionDiffItem, diff_option_set, get_option_set

HOLD_EXPIRED_MESSAGE = (
    "Your hold on that slot has lapsed. No booking has changed, and your original appointment "
    "still stands. I can search for fresh options if you would like."
)


class OutcomeClass(StrEnum):
    SUCCESS = "SUCCESS"
    BUSINESS_CONFLICT = "BUSINESS_CONFLICT"
    LOCK_TIMEOUT = "LOCK_TIMEOUT"
    SYSTEM_ERROR = "SYSTEM_ERROR"


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
    expires_at: datetime | None = None
    existing_appointment_id: str | None = None
    diff: OptionDiffItem | None = None


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
    return HoldResult(status=status, outcome=outcome, **kw)  # type: ignore[arg-type]


def _is_lock_error(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _fingerprint(**parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


# ---- expiry (shared by lazy expiry here and the sweep job) -------------------------------------
def expire_hold_in_txn(conn: sqlite3.Connection, appointment_id: str, now: datetime) -> bool:
    """HELD -> CANCELLED, capacity released, history appended, driver notice queued.

    Caller owns the transaction. Returns False if the row is no longer HELD (idempotent).
    """
    row = conn.execute(
        "select a.shipment_id, m.driver_id, m.thread_id from appointments a "
        "join appointment_hold_meta m using(appointment_id) "
        "where a.appointment_id=? and a.appointment_status='HELD'",
        (appointment_id,),
    ).fetchone()
    if row is None:
        return False
    stamp = now.isoformat()
    conn.execute(
        "update appointments set appointment_status='CANCELLED', cancelled_at=?, "
        "cancellation_reason='HOLD_EXPIRED', updated_at=? where appointment_id=? and appointment_status='HELD'",
        (stamp, stamp, appointment_id),
    )
    conn.execute(
        "update appointment_hold_meta set released_at=?, release_reason='EXPIRED' where appointment_id=?",
        (stamp, appointment_id),
    )
    event_id = append_appointment_event(
        conn, appointment_id, row["shipment_id"], "HOLD_EXPIRED", from_status="HELD", to_status="CANCELLED",
        actor_type="SYSTEM", actor_id=None, reason="HOLD_TTL_ELAPSED", occurred_at=now,
    )
    enqueue_notification(
        conn, f"HOLD_EXPIRED:{appointment_id}", "DRIVER", row["driver_id"], event_id,
        {"kind": "HOLD_EXPIRED", "thread_id": row["thread_id"], "appointment_id": appointment_id,
         "text": HOLD_EXPIRED_MESSAGE},
        now,
    )
    return True


def _expire_due_for(conn: sqlite3.Connection, now: datetime, *, shipment_id: str, slot_id: str) -> None:
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
) -> HoldResult:
    """Hold the slot shown at ``rank``. First successful commit wins (FCFS, decision D5)."""
    opt = get_option_set(conn, driver_id, thread_id, shipment_id, option_set_id)  # raises AuthError
    item = next((i for i in opt.items if i.rank == rank), None)
    if item is None:
        return _fail("INVALID_OPTION")
    ttl = timedelta(seconds=ttl_seconds if ttl_seconds is not None else load_settings().hold_ttl_seconds)
    try:
        with immediate(conn):
            now = clock.now()
            _expire_due_for(conn, now, shipment_id=shipment_id, slot_id=item.slot_id)
            if now >= opt.expires_at:
                return _fail("OPTION_EXPIRED")
            existing = conn.execute(
                "select appointment_id, slot_id from appointments where shipment_id=? and appointment_status='HELD'",
                (shipment_id,),
            ).fetchone()
            if existing is not None:
                if existing["slot_id"] == item.slot_id:
                    meta = conn.execute("select expires_at from appointment_hold_meta where appointment_id=?",
                                        (existing["appointment_id"],)).fetchone()
                    return HoldResult(
                        status="HELD", outcome=OutcomeClass.SUCCESS, appointment_id=existing["appointment_id"],
                        slot_id=item.slot_id, expires_at=parse_operational_timestamp(meta["expires_at"]),
                    )
                return _fail("SHIPMENT_ALREADY_HELD", existing_appointment_id=existing["appointment_id"])
            state = build_facility_state(conn, opt.facility_id, now)
            diff_item = next(i for i in diff_option_set(opt, now, state).items if i.rank == rank)
            if diff_item.change not in ("UNCHANGED", "CHANGED_STILL_FEASIBLE"):
                taken = diff_item.change in ("NEWLY_HELD", "NEWLY_OCCUPIED")
                return _fail("SLOT_CONFLICT" if taken else "OPTION_CHANGED", diff=diff_item)
            original = conn.execute(
                "select appointment_id from appointments where shipment_id=? and is_current=1 "
                "and appointment_status in ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')",
                (shipment_id,),
            ).fetchone()
            appointment_id = "APT-H-" + uuid.uuid4().hex[:10].upper()
            expires = now + ttl
            stamp = now.isoformat()
            conn.execute(
                "insert into appointments (appointment_id, shipment_id, slot_id, appointment_status, "
                "booking_source, is_current, booked_at, replaced_appointment_id, updated_at) "
                "values (?,?,?, 'HELD','DRIVER_CHAT',0,?,?,?)",
                (appointment_id, shipment_id, item.slot_id, stamp,
                 original["appointment_id"] if original else None, stamp),
            )
            conn.execute(
                "insert into appointment_hold_meta (appointment_id, driver_id, thread_id, option_set_id, "
                "expires_at, expires_epoch, request_fingerprint) values (?,?,?,?,?,?,?)",
                (appointment_id, driver_id, thread_id, option_set_id, expires.isoformat(),
                 int(expires.timestamp()),
                 _fingerprint(driver=driver_id, thread=thread_id, shipment=shipment_id,
                              option_set=option_set_id, slot=item.slot_id, slot_version=item.slot_version)),
            )
            append_appointment_event(
                conn, appointment_id, shipment_id, "HOLD_ACQUIRED", from_status=None, to_status="HELD",
                actor_type="DRIVER", actor_id=driver_id, reason="DRIVER_SELECTED_OPTION", occurred_at=now,
                details={"slot_id": item.slot_id, "option_set_id": option_set_id, "rank": rank,
                         "expires_at": expires.isoformat()},
            )
            return HoldResult(status="HELD", outcome=OutcomeClass.SUCCESS, appointment_id=appointment_id,
                              slot_id=item.slot_id, expires_at=expires)
    except sqlite3.IntegrityError:
        # The shared unique index fired: someone else's row won the slot (or the shipment's hold).
        return _fail("SLOT_CONFLICT")
    except sqlite3.OperationalError as exc:
        if _is_lock_error(exc):
            return _fail("LOCK_TIMEOUT", OutcomeClass.LOCK_TIMEOUT)
        return _fail("SYSTEM_ERROR", OutcomeClass.SYSTEM_ERROR)


# ---- release and inspection --------------------------------------------------------------------
def release_hold(
    conn: sqlite3.Connection, driver_id: str, thread_id: str, appointment_id: str, reason: str, clock: Clock
) -> ReleaseResult:
    if reason not in ("DRIVER_CANCELLED", "SUPERSEDED", "SYSTEM"):
        raise ValueError(f"unsupported release reason {reason!r}")
    with immediate(conn):
        row = conn.execute(
            "select a.shipment_id, a.appointment_status, m.driver_id, m.thread_id, m.released_at "
            "from appointments a join appointment_hold_meta m using(appointment_id) where a.appointment_id=?",
            (appointment_id,),
        ).fetchone()
        if row is None or row["driver_id"] != driver_id or row["thread_id"] != thread_id:
            raise AuthError("FOREIGN_HOLD")
        if row["appointment_status"] != "HELD":
            return ReleaseResult(status="ALREADY_RELEASED" if row["released_at"] else "NOT_HELD")
        now = clock.now()
        stamp = now.isoformat()
        conn.execute(
            "update appointments set appointment_status='CANCELLED', cancelled_at=?, "
            "cancellation_reason='HOLD_RELEASED', updated_at=? where appointment_id=?",
            (stamp, stamp, appointment_id),
        )
        conn.execute("update appointment_hold_meta set released_at=?, release_reason=? where appointment_id=?",
                     (stamp, reason, appointment_id))
        append_appointment_event(
            conn, appointment_id, row["shipment_id"], "HOLD_RELEASED", from_status="HELD", to_status="CANCELLED",
            actor_type="DRIVER" if reason == "DRIVER_CANCELLED" else "SYSTEM",
            actor_id=driver_id if reason == "DRIVER_CANCELLED" else None, reason=reason, occurred_at=now,
        )
    return ReleaseResult(status="RELEASED")


def get_hold(conn: sqlite3.Connection, appointment_id: str, clock: Clock) -> HoldView | None:
    r = conn.execute(
        "select a.appointment_id, a.shipment_id, a.slot_id, a.appointment_status, m.driver_id, m.thread_id, "
        "m.option_set_id, m.expires_at, m.expires_epoch, m.release_reason "
        "from appointments a join appointment_hold_meta m using(appointment_id) where a.appointment_id=?",
        (appointment_id,),
    ).fetchone()
    if r is None:
        return None
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
        if view is None:
            return "GONE"
        if view.state == "LIVE":
            return "LIVE"
        return "EXPIRED" if view.state == "EXPIRED" else "GONE"

    return check


# ---- shared in-transaction release ----------------------------------------------------------------
def release_hold_in_txn(
    conn: sqlite3.Connection, appointment_id: str, shipment_id: str, reason: str, now: datetime,
    actor_type: str, actor_id: str | None, event_reason: str,
) -> None:
    """HELD -> CANCELLED. Caller owns the transaction and has already checked ownership."""
    stamp = now.isoformat()
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
ACTION_CONFIRM = "CONFIRM_RESCHEDULE"
fault_hook: Callable[[str], None] | None = None  # tests inject failures at named write points


def _fault(point: str) -> None:
    if fault_hook is not None:
        fault_hook(point)


class ConfirmCommand(BaseModel):
    model_config = ConfigDict(frozen=True)

    driver_id: str
    thread_id: str
    message_id: str
    shipment_id: str
    hold_appointment_id: str

    def fingerprint(self) -> str:
        return _fingerprint(shipment=self.shipment_id, hold=self.hold_appointment_id, action=ACTION_CONFIRM)


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
    original_appointment_id: str | None = None
    reason_codes: tuple[str, ...] = ()
    replayed: bool = False


def _result(status: ConfirmStatus, **kw: object) -> ConfirmationResult:
    outcome = OutcomeClass.SUCCESS if status in ("CONFIRMED", "ALREADY_CONFIRMED") else OutcomeClass.BUSINESS_CONFLICT
    return ConfirmationResult(status=status, outcome=outcome, **kw)  # type: ignore[arg-type]


def get_idempotent_result(
    conn: sqlite3.Connection, driver_id: str, thread_id: str, message_id: str, action: str, fingerprint: str
) -> tuple[Literal["MISSING", "REPLAY", "MISMATCH"], ConfirmationResult | None]:
    row = conn.execute(
        "select fingerprint, result_json from idempotency_keys where driver_id=? and thread_id=? "
        "and message_id=? and action=?", (driver_id, thread_id, message_id, action),
    ).fetchone()
    if row is None:
        return "MISSING", None
    if row["fingerprint"] != fingerprint:
        return "MISMATCH", None
    return "REPLAY", ConfirmationResult.model_validate_json(row["result_json"])


def _store_result(conn: sqlite3.Connection, cmd: ConfirmCommand, result: ConfirmationResult, now: datetime) -> ConfirmationResult:
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
        with immediate(conn):
            now = clock.now()
            state_, stored = get_idempotent_result(
                conn, cmd.driver_id, cmd.thread_id, cmd.message_id, ACTION_CONFIRM, cmd.fingerprint())
            if state_ == "MISMATCH":
                return _result("IDEMPOTENCY_MISMATCH")
            if state_ == "REPLAY" and stored is not None:
                return stored.model_copy(update={"replayed": True})

            row = conn.execute(
                "select a.appointment_id, a.shipment_id, a.slot_id, a.appointment_status, m.driver_id, m.thread_id, "
                "m.option_set_id, m.expires_epoch, m.release_reason from appointments a "
                "join appointment_hold_meta m using(appointment_id) where a.appointment_id=?",
                (cmd.hold_appointment_id,),
            ).fetchone()
            if (row is None or row["driver_id"] != cmd.driver_id or row["thread_id"] != cmd.thread_id
                    or row["shipment_id"] != cmd.shipment_id):
                raise AuthError("FOREIGN_HOLD")
            if row["appointment_status"] == "CONFIRMED":
                return _store_result(conn, cmd, _result(
                    "ALREADY_CONFIRMED", appointment_id=row["appointment_id"], slot_id=row["slot_id"]), now)
            if row["appointment_status"] != "HELD":
                status: ConfirmStatus = "HOLD_EXPIRED" if row["release_reason"] == "EXPIRED" else "HOLD_NOT_ACTIVE"
                return _store_result(conn, cmd, _result(status), now)
            if row["expires_epoch"] <= int(now.timestamp()):
                expire_hold_in_txn(conn, cmd.hold_appointment_id, now)
                return _store_result(conn, cmd, _result("HOLD_EXPIRED"), now)

            opt = get_option_set(conn, cmd.driver_id, cmd.thread_id, cmd.shipment_id, row["option_set_id"])
            state = build_facility_state(conn, opt.facility_id, now)
            decision = evaluate_slot(opt.context.with_now(now), state.slots[row["slot_id"]], state)
            if decision.status != "FEASIBLE":
                release_hold_in_txn(conn, cmd.hold_appointment_id, cmd.shipment_id, "SYSTEM", now,
                                    "SYSTEM", None, "FEASIBILITY_LOST")
                kind: ConfirmStatus = "APPROVAL_REQUIRED" if decision.status == "REQUIRES_APPROVAL" else "FEASIBILITY_CHANGED"
                return _store_result(conn, cmd, _result(kind, reason_codes=decision.reasons or decision.missing), now)

            orig = conn.execute(
                "select appointment_id, appointment_status, slot_id from appointments where shipment_id=? "
                "and is_current=1 and appointment_status in ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')",
                (cmd.shipment_id,),
            ).fetchone()
            if orig is not None and orig["appointment_status"] == "IN_PROGRESS":
                release_hold_in_txn(conn, cmd.hold_appointment_id, cmd.shipment_id, "SYSTEM", now,
                                    "SYSTEM", None, "ORIGINAL_IN_PROGRESS")
                return _store_result(conn, cmd, _result("ORIGINAL_NOT_RESCHEDULABLE"), now)

            stamp = now.isoformat()
            _fault("BEFORE_END_ORIGINAL")
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
            conn.execute(
                "update appointments set appointment_status='CONFIRMED', is_current=1, confirmed_at=?, "
                "replaced_appointment_id=?, updated_at=? where appointment_id=? and appointment_status='HELD'",
                (stamp, orig["appointment_id"] if orig else None, stamp, cmd.hold_appointment_id),
            )
            _fault("AFTER_PROMOTE")
            conn.execute("update appointment_hold_meta set released_at=?, release_reason='CONFIRMED' where appointment_id=?",
                         (stamp, cmd.hold_appointment_id))
            _fault("AFTER_META")
            details = {"from_slot_id": orig["slot_id"] if orig else None, "to_slot_id": row["slot_id"],
                       "driver_id": cmd.driver_id}
            event_id = append_appointment_event(
                conn, cmd.hold_appointment_id, cmd.shipment_id, "RESCHEDULE_CONFIRMED", from_status="HELD",
                to_status="CONFIRMED", actor_type="DRIVER", actor_id=cmd.driver_id,
                reason="DRIVER_CONFIRMED_OPTION", occurred_at=now, details=details,
            )
            _fault("AFTER_EVENTS")
            enqueue_downstream(conn, event_id, "RESCHEDULE_CONFIRMED", cmd.shipment_id, cmd.hold_appointment_id,
                               {**details, "from_time": None, "to_time": decision.slot_start.isoformat()}, now)
            _fault("AFTER_OUTBOX")
            result = _result("CONFIRMED", appointment_id=cmd.hold_appointment_id, slot_id=row["slot_id"],
                             original_appointment_id=orig["appointment_id"] if orig else None)
            _store_result(conn, cmd, result, now)
            _fault("AFTER_IDEMPOTENCY")
            return result
    except AuthError:
        raise
    except sqlite3.OperationalError as exc:
        if _is_lock_error(exc):
            return ConfirmationResult(status="LOCK_TIMEOUT", outcome=OutcomeClass.LOCK_TIMEOUT)
        return ConfirmationResult(status="SYSTEM_ERROR", outcome=OutcomeClass.SYSTEM_ERROR)
    except Exception:  # noqa: BLE001 - any failure must roll back and be reported as SYSTEM_ERROR, never raised to the driver turn
        return ConfirmationResult(status="SYSTEM_ERROR", outcome=OutcomeClass.SYSTEM_ERROR)


def confirmed_appointment_facts(conn: sqlite3.Connection, appointment_id: str) -> dict[str, str]:
    """Facts of a committed CONFIRMED appointment. Raises ValueError for anything else."""
    r = conn.execute(
        "select a.appointment_id, a.appointment_status, s.slot_start_ts, s.slot_end_ts, d.dock_code "
        "from appointments a join appointment_slots s using(slot_id) join docks d on d.dock_id = s.dock_id "
        "where a.appointment_id=?", (appointment_id,),
    ).fetchone()
    if r is None:
        raise ValueError(f"no such appointment {appointment_id}")
    if r["appointment_status"] != "CONFIRMED":
        raise ValueError(f"appointment {appointment_id} is not confirmed ({r['appointment_status']})")
    start = parse_operational_timestamp(r["slot_start_ts"])
    end = parse_operational_timestamp(r["slot_end_ts"])
    return {"dock": r["dock_code"], "start": f"{start:%H:%M}", "end": f"{end:%H:%M}",
            "date": f"{start:%d %b %Y}", "ref": r["appointment_id"]}


def render_confirmation_text(conn: sqlite3.Connection, appointment_id: str) -> str:
    """English wording for a confirmed booking, built only from the committed row."""
    f = confirmed_appointment_facts(conn, appointment_id)
    return (f"Your appointment is confirmed: dock {f['dock']}, {f['start']}-{f['end']} on "
            f"{f['date']}. Reference {f['ref']}.")
