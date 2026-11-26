"""Confirming an assigned shipment by its Dock Access Pass. The app decides this; the model never sees the code or the shipment first."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict

from setuhaul.clock import Clock
from setuhaul.db import immediate, parse_operational_timestamp
from setuhaul.dock_pass import find, guesses, hash_code
from setuhaul.escalation import EscalationSink, HandoffRequest
from setuhaul.language import say
from setuhaul.passes import LIVE

# Wrong codes allowed before confirmation locks.
MAX_ATTEMPTS = 5
# The driver's shipments, each with its assignment and the hash of its live pass if it has them.
_MINE = ("select s.shipment_id, s.order_reference, s.original_eta_ts, s.current_status, f.facility_name, a.shipment_id as assigned,"
         " a.confirmed_at, a.locked_at, a.failed_attempts, (select p.token_hash from dock_access_authorizations p"
         " join visit_shipments v using(truck_visit_id) where v.shipment_id=s.shipment_id and p.driver_id=s.driver_id"
         f" and p.status in {LIVE}) as live_hash"
         " from shipments s join facilities f on f.facility_id=s.destination_facility_id"
         " left join shipment_assignments a on a.shipment_id=s.shipment_id where s.driver_id=? order by s.shipment_id")


class PassReply(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["CONFIRMED", "MISMATCH", "LOCKED"]
    text: str
    case_id: str | None = None


def _waiting(row: sqlite3.Row) -> bool:
    return row["assigned"] is not None and row["confirmed_at"] is None and row["current_status"] != "CANCELLED"


def has_waiting(conn: sqlite3.Connection, driver_id: str) -> bool:
    return any(_waiting(r) for r in conn.execute(_MINE, (driver_id,)))


def prompt_key(conn: sqlite3.Connection, driver_id: str) -> str | None:
    """The line to add to an ordinary reply: ask for the pass, or send the driver to the warehouse once all are locked."""
    waiting = [r for r in conn.execute(_MINE, (driver_id,)) if _waiting(r)]
    if not waiting:
        return None
    return "pass_waiting" if any(r["locked_at"] is None for r in waiting) else "pass_locked"


def check(conn: sqlite3.Connection, clock: Clock, *, driver_id: str, thread_id: str, text: str, lang: str,
          sink: EscalationSink) -> PassReply | None:
    """What to answer if this message is about confirming a waiting shipment; None leaves it to the conversation."""
    found = {hash_code(code) for code in find(text)}
    if not found:
        return None
    newly_locked: str | None = None
    confirmed: list[sqlite3.Row] = []
    with immediate(conn):
        rows = conn.execute(_MINE, (driver_id,)).fetchall()
        waiting = [r for r in rows if _waiting(r)]
        if not waiting:
            return None
        named = [r for r in waiting if r["live_hash"] in found]
        # Every different code that is none of this driver's live passes is one guess, however many arrive in one message.
        wrong = {hash_code(code) for code in guesses(text)} - {r["live_hash"] for r in rows}
        now = clock.now().isoformat()
        for r in waiting:
            if r["locked_at"] is not None:
                continue
            attempts = min(r["failed_attempts"] + len(wrong), MAX_ATTEMPTS)
            if wrong and attempts >= MAX_ATTEMPTS:
                # Too many guesses: a right code among them does not count.
                conn.execute("update shipment_assignments set failed_attempts=?, locked_at=? where shipment_id=?",
                             (attempts, now, r["shipment_id"]))
                newly_locked = newly_locked or r["shipment_id"]
            elif r in named:
                conn.execute("update shipment_assignments set confirmed_at=? where shipment_id=?", (now, r["shipment_id"]))
                confirmed.append(r)
            elif wrong:
                conn.execute("update shipment_assignments set failed_attempts=? where shipment_id=?", (attempts, r["shipment_id"]))
    if confirmed:
        text_out = "\n".join(say("pass_confirmed", lang, order=r["order_reference"], facility=r["facility_name"],
                                 when=f"{parse_operational_timestamp(r['original_eta_ts']):%d %b %Y %H:%M}") for r in confirmed)
        kind: Literal["CONFIRMED", "MISMATCH", "LOCKED"] = "CONFIRMED"
    elif newly_locked or named or all(r["locked_at"] is not None for r in waiting):
        # Repeating a pass that is already theirs is not about a locked shipment.
        if not newly_locked and not named and not wrong:
            return None
        text_out, kind = say("pass_locked", lang), "LOCKED"
    elif wrong:
        text_out, kind = say("pass_mismatch", lang), "MISMATCH"
    else:
        return None
    if newly_locked is None:
        return PassReply(kind=kind, text=text_out)
    # Recorded after the lock is committed: the sink opens its own transaction. It names a shipment so that warehouse sees it.
    case = sink.record(HandoffRequest(
        reason_code="PASS_CONFIRMATION_LOCKED", driver_id=driver_id, thread_id=thread_id, shipment_id=newly_locked,
        summary=f"Pass confirmation locked after {MAX_ATTEMPTS} wrong codes.", created_at=clock.now()))
    return PassReply(kind=kind, text=text_out, case_id=case)
