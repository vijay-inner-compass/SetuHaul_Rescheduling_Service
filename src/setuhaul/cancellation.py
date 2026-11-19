"""Cancellation and reversal as explicit workflows.

* "Never mind" before confirmation releases the hold; the original appointment is untouched.
* Cancelling a confirmed appointment is allowed only outside the cutoff and when no customer
  commitment (known or unknown) is at stake; otherwise it escalates and changes nothing.
* Returning to the original slot after a confirmed reschedule is a NEW request: the old slot may
  have been allocated, so it is re-evaluated and offered like any other option, never restored.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

from setuhaul.allocation import RankedOption
from setuhaul.booking import OutcomeClass, _fingerprint, _is_lock_error, release_hold_in_txn
from setuhaul.capacity import build_facility_state, load_consignee
from setuhaul.clock import Clock
from setuhaul.config import load_settings
from setuhaul.db import immediate, parse_operational_timestamp
from setuhaul.events import append_appointment_event
from setuhaul.feasibility import build_context, evaluate_slot
from setuhaul.identity import AuthError
from setuhaul.notifications import enqueue_downstream
from setuhaul.option_history import OptionSet, record_option_set
from setuhaul.schemas import EtaEvidence

ACTION_CANCEL = "CANCEL_APPOINTMENT"
_ACTIVE = "('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')"


def _require_shipment(conn: sqlite3.Connection, driver_id: str, shipment_id: str) -> sqlite3.Row:
    row = conn.execute("select * from shipments where shipment_id=? and driver_id=?", (shipment_id, driver_id)).fetchone()
    if row is None:
        raise AuthError("FOREIGN_SHIPMENT")
    assert isinstance(row, sqlite3.Row)
    return row


# ---- never mind (pending change) ---------------------------------------------------------------------
class PendingCancelResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["RELEASED", "NOTHING_PENDING"]
    appointment_id: str | None = None


def cancel_pending_change(
    conn: sqlite3.Connection, driver_id: str, thread_id: str, shipment_id: str, clock: Clock
) -> PendingCancelResult:
    _require_shipment(conn, driver_id, shipment_id)
    with immediate(conn):
        row = conn.execute(
            "select a.appointment_id from appointments a join appointment_hold_meta m using(appointment_id) "
            "where a.shipment_id=? and a.appointment_status='HELD' and m.driver_id=?", (shipment_id, driver_id),
        ).fetchone()
        if row is None:
            return PendingCancelResult(status="NOTHING_PENDING")
        release_hold_in_txn(conn, row["appointment_id"], shipment_id, "DRIVER_CANCELLED", clock.now(),
                            "DRIVER", driver_id, "DRIVER_CANCELLED")
    return PendingCancelResult(status="RELEASED", appointment_id=row["appointment_id"])


# ---- cancel a confirmed appointment ---------------------------------------------------------------------
CancelStatus = Literal[
    "CANCELLED", "ESCALATE", "NOT_CANCELLABLE", "NOT_FOUND", "IDEMPOTENCY_MISMATCH", "LOCK_TIMEOUT", "SYSTEM_ERROR"
]


class CancellationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: CancelStatus
    outcome: OutcomeClass
    appointment_id: str | None = None
    escalation_code: Literal["CANCELLATION_CUTOFF", "CUSTOMER_COMMITMENT", "CUSTOMER_COMMITMENT_UNKNOWN"] | None = None
    released_hold_id: str | None = None
    replayed: bool = False


def _res(status: CancelStatus, **kw: object) -> CancellationResult:
    ok = status in ("CANCELLED", "ESCALATE", "NOT_CANCELLABLE", "NOT_FOUND")
    outcome = OutcomeClass.SUCCESS if status == "CANCELLED" else (
        OutcomeClass.BUSINESS_CONFLICT if ok or status == "IDEMPOTENCY_MISMATCH" else OutcomeClass.SYSTEM_ERROR)
    return CancellationResult(status=status, outcome=outcome, **kw)  # type: ignore[arg-type]


def cancel_confirmed_appointment(
    conn: sqlite3.Connection, driver_id: str, thread_id: str, message_id: str, shipment_id: str, clock: Clock
) -> CancellationResult:
    _require_shipment(conn, driver_id, shipment_id)
    fp = _fingerprint(shipment=shipment_id, action=ACTION_CANCEL)
    try:
        with immediate(conn):
            now = clock.now()
            prior = conn.execute(
                "select fingerprint, result_json from idempotency_keys where driver_id=? and thread_id=? "
                "and message_id=? and action=?", (driver_id, thread_id, message_id, ACTION_CANCEL),
            ).fetchone()
            if prior is not None:
                if prior["fingerprint"] != fp:
                    return _res("IDEMPOTENCY_MISMATCH")
                return CancellationResult.model_validate_json(prior["result_json"]).model_copy(update={"replayed": True})

            def store(result: CancellationResult) -> CancellationResult:
                conn.execute(
                    "insert into idempotency_keys (driver_id, thread_id, message_id, action, fingerprint, "
                    "result_json, created_at) values (?,?,?,?,?,?,?)",
                    (driver_id, thread_id, message_id, ACTION_CANCEL, fp, result.model_dump_json(), now.isoformat()))
                return result

            appt = conn.execute(
                f"select a.appointment_id, a.appointment_status, s.slot_start_ts from appointments a "
                f"join appointment_slots s using(slot_id) where a.shipment_id=? and a.is_current=1 "
                f"and a.appointment_status in {_ACTIVE}", (shipment_id,),
            ).fetchone()
            if appt is None:
                return store(_res("NOT_FOUND"))
            if appt["appointment_status"] == "IN_PROGRESS":
                return store(_res("NOT_CANCELLABLE", appointment_id=appt["appointment_id"]))
            cutoff = timedelta(minutes=load_settings().cancel_cutoff_minutes)
            if parse_operational_timestamp(appt["slot_start_ts"]) - now < cutoff:
                return store(_res("ESCALATE", appointment_id=appt["appointment_id"], escalation_code="CANCELLATION_CUTOFF"))
            commitment = load_consignee(conn, shipment_id)
            if commitment is None:
                return store(_res("ESCALATE", appointment_id=appt["appointment_id"],
                                  escalation_code="CUSTOMER_COMMITMENT_UNKNOWN"))
            if commitment.penalty_applies or commitment.exceptional:
                return store(_res("ESCALATE", appointment_id=appt["appointment_id"], escalation_code="CUSTOMER_COMMITMENT"))

            stamp = now.isoformat()
            held = conn.execute(
                "select appointment_id from appointments where shipment_id=? and appointment_status='HELD'", (shipment_id,)
            ).fetchone()
            if held is not None:
                release_hold_in_txn(conn, held["appointment_id"], shipment_id, "SYSTEM", now, "SYSTEM", None,
                                    "ORIGINAL_CANCELLED")
            conn.execute(
                "update appointments set appointment_status='CANCELLED', is_current=0, cancelled_at=?, "
                "cancellation_reason='DRIVER_CANCELLED', updated_at=? where appointment_id=?",
                (stamp, stamp, appt["appointment_id"]),
            )
            event_id = append_appointment_event(
                conn, appt["appointment_id"], shipment_id, "APPOINTMENT_CANCELLED",
                from_status=appt["appointment_status"], to_status="CANCELLED", actor_type="DRIVER",
                actor_id=driver_id, reason="DRIVER_CANCELLED", occurred_at=now,
            )
            enqueue_downstream(conn, event_id, "APPOINTMENT_CANCELLED", shipment_id, appt["appointment_id"],
                               {"driver_id": driver_id}, now)
            return store(_res("CANCELLED", appointment_id=appt["appointment_id"],
                              released_hold_id=held["appointment_id"] if held else None))
    except AuthError:
        raise
    except sqlite3.OperationalError as exc:
        if _is_lock_error(exc):
            return CancellationResult(status="LOCK_TIMEOUT", outcome=OutcomeClass.LOCK_TIMEOUT)
        return CancellationResult(status="SYSTEM_ERROR", outcome=OutcomeClass.SYSTEM_ERROR)
    except Exception:
        return CancellationResult(status="SYSTEM_ERROR", outcome=OutcomeClass.SYSTEM_ERROR)


# ---- return to the original slot ---------------------------------------------------------------------------
class OriginalSlotResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["OPTION_AVAILABLE", "NO_LONGER_AVAILABLE", "NOT_FEASIBLE", "NO_PREVIOUS_SLOT",
                    "INCOMPLETE_FEASIBILITY_DATA"]
    option_set: OptionSet | None = None
    reason_codes: tuple[str, ...] = ()


def request_original_slot_again(
    conn: sqlite3.Connection, driver_id: str, thread_id: str, shipment_id: str, eta: EtaEvidence, clock: Clock
) -> OriginalSlotResult:
    """Re-evaluate the slot that a confirmed reschedule replaced. Offers it; never restores it."""
    _require_shipment(conn, driver_id, shipment_id)
    prev = conn.execute(
        f"select o.slot_id from appointments cur join appointments o on o.appointment_id = cur.replaced_appointment_id "
        f"where cur.shipment_id=? and cur.is_current=1 and cur.appointment_status in {_ACTIVE}", (shipment_id,),
    ).fetchone()
    if prev is None:
        return OriginalSlotResult(status="NO_PREVIOUS_SLOT")
    now: datetime = clock.now()
    ctx = build_context(conn, shipment_id, eta, now)
    state = build_facility_state(conn, ctx.facility_id, now)
    decision = evaluate_slot(ctx, state.slots[prev["slot_id"]], state)
    if decision.status == "FEASIBLE":
        shown = record_option_set(conn, ctx, thread_id, [RankedOption(rank=1, decision=decision)], state, clock)
        return OriginalSlotResult(status="OPTION_AVAILABLE", option_set=shown)
    codes = decision.reasons or decision.missing
    if decision.status == "INCOMPLETE_FEASIBILITY_DATA":
        return OriginalSlotResult(status="INCOMPLETE_FEASIBILITY_DATA", reason_codes=codes)
    if {"SLOT_OCCUPIED", "SLOT_HELD"} & set(codes):
        return OriginalSlotResult(status="NO_LONGER_AVAILABLE", reason_codes=codes)
    return OriginalSlotResult(status="NOT_FEASIBLE", reason_codes=codes)
