"""Cancellation and reversal as explicit workflows.

* "Never mind" before confirmation releases the hold; the original appointment is untouched.
* Cancelling a confirmed appointment is allowed only outside the cutoff and when no customer
  commitment (known or unknown) is at stake; otherwise it escalates and changes nothing.
* Returning to the original slot after a confirmed reschedule is a NEW request: the old slot may
  have been allocated, so it is re-evaluated and offered like any other option, never restored.
"""

# Lazy annotations.
from __future__ import annotations

# sqlite3 for the Connection type.
import logging
import sqlite3

# datetime/timedelta for times and the cut-off.
from datetime import datetime, timedelta

# Literal restricts result statuses.
from typing import Literal

# Frozen validated models.
from pydantic import BaseModel, ConfigDict

# A ranked option, used to present the original slot as a single option.
from setuhaul import passes
from setuhaul.allocation import RankedOption

# Shared helpers from booking: outcome classes, fingerprinting, lock-error detection and in-transaction release.
from setuhaul.booking import OutcomeClass, _fingerprint, _is_lock_error, release_hold_in_txn

# Capacity snapshot and consignee commitment loaders.
from setuhaul.capacity import build_facility_state, load_consignee

# The injectable clock.
from setuhaul.clock import Clock

# Settings give the cancellation cut-off.
from setuhaul.config import load_settings

# immediate(): the write transaction; parse_operational_timestamp for stored times.
from setuhaul.db import immediate, parse_operational_timestamp

# History writer.
from setuhaul.events import append_appointment_event

# Feasibility context and per-slot evaluation.
from setuhaul.feasibility import build_context, evaluate_slot

# Authorisation error.
from setuhaul.identity import AuthError

# Downstream notices (planner, consignee, operations).
from setuhaul.notifications import enqueue_downstream
from setuhaul.observability import log_failure

# Stored menus.
from setuhaul.option_history import OptionSet, record_option_set

# The ETA evidence type.
from setuhaul.schemas import EtaEvidence

# The idempotency "action" name for cancellations, so a cancel and a confirm with the same message id never collide.
_log = logging.getLogger(__name__)

ACTION_CANCEL = "CANCEL_APPOINTMENT"
# Statuses of a real, live appointment (a HELD proposal is not included).
_ACTIVE = "('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')"


def _require_shipment(conn: sqlite3.Connection, driver_id: str, shipment_id: str) -> sqlite3.Row:
    # The shipment must exist and belong to the driver; the same error covers both cases so ids cannot be probed.
    row = conn.execute("select * from v_driver_shipments where shipment_id=? and driver_id=?", (shipment_id, driver_id)).fetchone()
    if row is None:
        raise AuthError("FOREIGN_SHIPMENT")
    assert isinstance(row, sqlite3.Row)
    return row


# ---- never mind (pending change) ---------------------------------------------------------------------
class PendingCancelResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    # RELEASED: a hold was found and released. NOTHING_PENDING: there was no hold to release.
    status: Literal["RELEASED", "NOTHING_PENDING"]
    appointment_id: str | None = None


def cancel_pending_change(
    conn: sqlite3.Connection, driver_id: str, thread_id: str, shipment_id: str, clock: Clock
) -> PendingCancelResult:
    # Ownership check first.
    _require_shipment(conn, driver_id, shipment_id)
    with immediate(conn):
        # The driver's own hold on this shipment, if any.
        row = conn.execute(
            "select a.appointment_id from appointments a join appointment_hold_meta m using(appointment_id) "
            "where a.shipment_id=? and a.appointment_status='HELD' and m.driver_id=?", (shipment_id, driver_id),
        ).fetchone()
        # No hold: saying so is a normal answer, not an error ("never mind" when nothing was pending).
        if row is None:
            return PendingCancelResult(status="NOTHING_PENDING")
        # Release the hold. The original appointment is not touched, so "never mind" can never lose the driver's booking.
        release_hold_in_txn(conn, row["appointment_id"], shipment_id, "DRIVER_CANCELLED", clock.now(),
                            "DRIVER", driver_id, "DRIVER_CANCELLED")
    return PendingCancelResult(status="RELEASED", appointment_id=row["appointment_id"])


# ---- cancel a confirmed appointment ---------------------------------------------------------------------
# All the ways a cancellation request can end.
CancelStatus = Literal[
    "CANCELLED", "ESCALATE", "NOT_CANCELLABLE", "NOT_FOUND", "IDEMPOTENCY_MISMATCH", "LOCK_TIMEOUT", "SYSTEM_ERROR"
]


class CancellationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: CancelStatus
    outcome: OutcomeClass
    appointment_id: str | None = None
    # Why a cancellation was handed to humans instead of being done automatically.
    escalation_code: Literal["CANCELLATION_CUTOFF", "CUSTOMER_COMMITMENT", "CUSTOMER_COMMITMENT_UNKNOWN"] | None = None
    # A hold that was released as part of the cancellation, if any.
    released_hold_id: str | None = None
    # True when this is the stored answer to a repeated request.
    replayed: bool = False


def _res(status: CancelStatus, **kw: object) -> CancellationResult:
    # "ok" statuses are expected business answers, not system failures.
    ok = status in ("CANCELLED", "ESCALATE", "NOT_CANCELLABLE", "NOT_FOUND")
    # CANCELLED is a success; other expected answers (and an idempotency clash) are business conflicts; the rest are system errors.
    outcome = OutcomeClass.SUCCESS if status == "CANCELLED" else (
        OutcomeClass.BUSINESS_CONFLICT if ok or status == "IDEMPOTENCY_MISMATCH" else OutcomeClass.SYSTEM_ERROR)
    return CancellationResult(status=status, outcome=outcome, **kw)  # type: ignore[arg-type]


# The rules for cancelling by chat: the current appointment, and why it may not be cancelled (None: it may).
def _judge(conn: sqlite3.Connection, shipment_id: str, now: datetime) -> tuple[sqlite3.Row | None, CancellationResult | None]:
    # The shipment's current live appointment.
    appt = conn.execute(
        f"select a.appointment_id, a.appointment_status, a.slot_id, s.slot_start_ts from appointments a "
        f"join appointment_slots s using(slot_id) where a.shipment_id=? and a.is_current=1 "
        f"and a.appointment_status in {_ACTIVE}", (shipment_id,),
    ).fetchone()
    # The truck is through the gate: cancelling here would free a dock it is using.
    if appt is not None and passes.on_site(conn, shipment_id):
        return appt, _res("NOT_CANCELLABLE", appointment_id=appt["appointment_id"])
    # Nothing to cancel.
    if appt is None:
        return None, _res("NOT_FOUND")
    # Unloading has started: it cannot be cancelled by chat.
    if appt["appointment_status"] == "IN_PROGRESS":
        return appt, _res("NOT_CANCELLABLE", appointment_id=appt["appointment_id"])
    # Too close to the start time (provisional 120 minutes): a human must decide.
    cutoff = timedelta(minutes=load_settings().cancel_cutoff_minutes)
    if parse_operational_timestamp(appt["slot_start_ts"]) - now < cutoff:
        return appt, _res("ESCALATE", appointment_id=appt["appointment_id"], escalation_code="CANCELLATION_CUTOFF")
    # The customer's commitment decides whether cancelling harms them.
    commitment = load_consignee(conn, shipment_id)
    # Unknown commitment: do not assume it is harmless.
    if commitment is None:
        return appt, _res("ESCALATE", appointment_id=appt["appointment_id"], escalation_code="CUSTOMER_COMMITMENT_UNKNOWN")
    # A penalty or exceptional delivery: a human decides.
    if commitment.penalty_applies or commitment.exceptional:
        return appt, _res("ESCALATE", appointment_id=appt["appointment_id"], escalation_code="CUSTOMER_COMMITMENT")
    return appt, None


# Look before asking the driver "cancel?": the appointment that would be cancelled, or the rules' refusal. Writes nothing.
def preview_cancellation(
    conn: sqlite3.Connection, driver_id: str, shipment_id: str, clock: Clock
) -> tuple[sqlite3.Row | None, CancellationResult | None]:
    _require_shipment(conn, driver_id, shipment_id)
    return _judge(conn, shipment_id, clock.now())


def cancel_confirmed_appointment(
    conn: sqlite3.Connection, driver_id: str, thread_id: str, message_id: str, shipment_id: str, clock: Clock
) -> CancellationResult:
    _require_shipment(conn, driver_id, shipment_id)
    # What identifies "the same cancel request": which shipment and which action.
    fp = _fingerprint(shipment=shipment_id, action=ACTION_CANCEL)
    try:
        with immediate(conn):
            now = clock.now()
            # Has this exact message already been processed? If so, return the stored answer instead of acting twice.
            prior = conn.execute(
                "select fingerprint, result_json from idempotency_keys where driver_id=? and thread_id=? "
                "and message_id=? and action=?", (driver_id, thread_id, message_id, ACTION_CANCEL),
            ).fetchone()
            if prior is not None:
                # Same message id but a different request: refuse; it is a client bug or a replay attack.
                if prior["fingerprint"] != fp:
                    return _res("IDEMPOTENCY_MISMATCH")
                return CancellationResult.model_validate_json(prior["result_json"]).model_copy(update={"replayed": True})

            def store(result: CancellationResult) -> CancellationResult:
                # Save the answer in the same transaction, so a retry of this message gets the same answer.
                conn.execute(
                    "insert into idempotency_keys (driver_id, thread_id, message_id, action, fingerprint, "
                    "result_json, created_at) values (?,?,?,?,?,?,?)",
                    (driver_id, thread_id, message_id, ACTION_CANCEL, fp, result.model_dump_json(), now.isoformat()))
                return result

            # The rules decide first; a refusal is stored like any other answer.
            appt, refusal = _judge(conn, shipment_id, now)
            if refusal is not None:
                return store(refusal)
            assert appt is not None
            # Safe to cancel automatically.
            stamp = now.isoformat()
            # A pending hold for the same shipment would be orphaned by this cancellation, so release it too.
            held = conn.execute(
                "select appointment_id from appointments where shipment_id=? and appointment_status='HELD'", (shipment_id,)
            ).fetchone()
            if held is not None:
                release_hold_in_txn(conn, held["appointment_id"], shipment_id, "SYSTEM", now, "SYSTEM", None,
                                    "ORIGINAL_CANCELLED")
            # End the appointment and mark it no longer current.
            conn.execute(
                "update appointments set appointment_status='CANCELLED', is_current=0, cancelled_at=?, "
                "cancellation_reason='DRIVER_CANCELLED', updated_at=? where appointment_id=?",
                (stamp, stamp, appt["appointment_id"]),
            )
            # History entry, then notices to planner, consignee and operations, all in this same transaction.
            event_id = append_appointment_event(
                conn, appt["appointment_id"], shipment_id, "APPOINTMENT_CANCELLED",
                from_status=appt["appointment_status"], to_status="CANCELLED", actor_type="DRIVER",
                actor_id=driver_id, reason="DRIVER_CANCELLED", occurred_at=now,
            )
            enqueue_downstream(conn, event_id, "APPOINTMENT_CANCELLED", shipment_id, appt["appointment_id"],
                               {"driver_id": driver_id}, now)
            return store(_res("CANCELLED", appointment_id=appt["appointment_id"],
                              released_hold_id=held["appointment_id"] if held else None))
    # Authorisation errors are not "system errors": let them reach the caller.
    except AuthError:
        raise
    # A busy database is reported separately from other database errors.
    except sqlite3.OperationalError as exc:
        if _is_lock_error(exc):
            return CancellationResult(status="LOCK_TIMEOUT", outcome=OutcomeClass.LOCK_TIMEOUT)
        log_failure(_log, "cancel appointment", exc)
        return CancellationResult(status="SYSTEM_ERROR", outcome=OutcomeClass.SYSTEM_ERROR)
    # Anything else: the transaction has already rolled back, so report a system error and promise nothing.
    except Exception as exc:  # noqa: BLE001 - any failure must roll back and be reported as SYSTEM_ERROR
        log_failure(_log, "cancel appointment", exc)
        return CancellationResult(status="SYSTEM_ERROR", outcome=OutcomeClass.SYSTEM_ERROR)


# ---- return to the original slot ---------------------------------------------------------------------------
class OriginalSlotResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["OPTION_AVAILABLE", "NO_LONGER_AVAILABLE", "NOT_FEASIBLE", "NO_PREVIOUS_SLOT",
                    "INCOMPLETE_FEASIBILITY_DATA"]
    # The single-option menu when the old slot is still usable.
    option_set: OptionSet | None = None
    reason_codes: tuple[str, ...] = ()


def request_original_slot_again(
    conn: sqlite3.Connection, driver_id: str, thread_id: str, shipment_id: str, eta: EtaEvidence, clock: Clock
) -> OriginalSlotResult:
    """Re-evaluate the slot that a confirmed reschedule replaced. Offers it; never restores it."""
    _require_shipment(conn, driver_id, shipment_id)
    # The slot of the appointment that the current one replaced.
    prev = conn.execute(
        f"select o.slot_id from appointments cur join appointments o on o.appointment_id = cur.replaced_appointment_id "
        f"where cur.shipment_id=? and cur.is_current=1 and cur.appointment_status in {_ACTIVE}", (shipment_id,),
    ).fetchone()
    # The shipment was never rescheduled, so there is no "original" to return to.
    if prev is None:
        return OriginalSlotResult(status="NO_PREVIOUS_SLOT")
    now: datetime = clock.now()
    # Judge the old slot exactly like any other candidate, since it may have been given to someone else meanwhile.
    ctx = build_context(conn, shipment_id, eta, now)
    state = build_facility_state(conn, ctx.facility_id, now)
    decision = evaluate_slot(ctx, state.slots[prev["slot_id"]], state)
    # Still feasible: offer it as a one-line menu. The driver must still hold and confirm it like any option.
    if decision.status == "FEASIBLE":
        shown = record_option_set(conn, ctx, thread_id, [RankedOption(rank=1, decision=decision)], state, clock)
        return OriginalSlotResult(status="OPTION_AVAILABLE", option_set=shown)
    codes = decision.reasons or decision.missing
    # Missing evidence.
    if decision.status == "INCOMPLETE_FEASIBILITY_DATA":
        return OriginalSlotResult(status="INCOMPLETE_FEASIBILITY_DATA", reason_codes=codes)
    # Someone else holds or has booked it.
    if {"SLOT_OCCUPIED", "SLOT_HELD"} & set(codes):
        return OriginalSlotResult(status="NO_LONGER_AVAILABLE", reason_codes=codes)
    # Infeasible for another reason (for example, the ETA is now too late for it).
    return OriginalSlotResult(status="NOT_FEASIBLE", reason_codes=codes)
