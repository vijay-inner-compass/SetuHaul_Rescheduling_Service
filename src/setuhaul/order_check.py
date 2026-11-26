"""Confirming an assigned shipment by its order number. The app decides this; the model never sees the shipment first."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict

from setuhaul.clock import Clock
from setuhaul.db import immediate, parse_operational_timestamp
from setuhaul.escalation import EscalationSink, HandoffRequest
from setuhaul.language import say
from setuhaul.order_numbers import mentions, normalise

# Wrong numbers allowed before confirmation locks.
MAX_ATTEMPTS = 5
# The driver's shipments, with each one's assignment if it has one.
_MINE = ("select s.shipment_id, s.order_reference, s.original_eta_ts, s.current_status, f.facility_name, a.shipment_id as assigned,"
         " a.confirmed_at, a.locked_at, a.failed_attempts from shipments s join facilities f on f.facility_id=s.destination_facility_id"
         " left join shipment_assignments a on a.shipment_id=s.shipment_id where s.driver_id=? order by s.shipment_id")


class OrderReply(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["CONFIRMED", "MISMATCH", "LOCKED"]
    text: str
    case_id: str | None = None


def _waiting(row: sqlite3.Row) -> bool:
    return row["assigned"] is not None and row["confirmed_at"] is None and row["current_status"] != "CANCELLED"


def has_waiting(conn: sqlite3.Connection, driver_id: str) -> bool:
    return any(_waiting(r) for r in conn.execute(_MINE, (driver_id,)))


def check(conn: sqlite3.Connection, clock: Clock, *, driver_id: str, thread_id: str, text: str, lang: str,
          sink: EscalationSink) -> OrderReply | None:
    """What to answer if this message is about confirming a waiting shipment; None leaves it to the conversation."""
    found = mentions(text)
    if not found:
        return None
    newly_locked: str | None = None
    with immediate(conn):
        rows = conn.execute(_MINE, (driver_id,)).fetchall()
        waiting = [r for r in rows if _waiting(r)]
        if not waiting:
            return None
        named = [r for r in waiting if any(normalise(r["order_reference"]) in m for m in found)]
        matched = [r for r in named if r["locked_at"] is None]
        if matched:
            now = clock.now().isoformat()
            for r in matched:
                conn.execute("update shipment_assignments set confirmed_at=? where shipment_id=?", (now, r["shipment_id"]))
            lines = [say("order_confirmed", lang, order=r["order_reference"], facility=r["facility_name"],
                         when=f"{parse_operational_timestamp(r['original_eta_ts']):%d %b %Y %H:%M}") for r in matched]
            return OrderReply(kind="CONFIRMED", text="\n".join(lines))
        if named:
            return OrderReply(kind="LOCKED", text=say("order_locked", lang))
        # Repeating a number that is already theirs is not a wrong guess.
        known = [normalise(r["order_reference"]) for r in rows if not _waiting(r)]
        if all(any(k in m for k in known) for m in found):
            return None
        open_ = [r for r in waiting if r["locked_at"] is None]
        if not open_:
            return OrderReply(kind="LOCKED", text=say("order_locked", lang))
        for r in open_:
            attempts = r["failed_attempts"] + 1
            locked = clock.now().isoformat() if attempts >= MAX_ATTEMPTS else None
            conn.execute("update shipment_assignments set failed_attempts=?, locked_at=? where shipment_id=?",
                         (attempts, locked, r["shipment_id"]))
            if locked and newly_locked is None:
                newly_locked = r["shipment_id"]
    if newly_locked is None:
        return OrderReply(kind="MISMATCH", text=say("order_mismatch", lang))
    # Recorded after the lock is committed: the sink opens its own transaction. It names a shipment so that warehouse sees it.
    case = sink.record(HandoffRequest(
        reason_code="ORDER_CONFIRMATION_LOCKED", driver_id=driver_id, thread_id=thread_id, shipment_id=newly_locked,
        summary=f"Order confirmation locked after {MAX_ATTEMPTS} wrong order numbers.", created_at=clock.now()))
    return OrderReply(kind="LOCKED", text=say("order_locked", lang), case_id=case)
