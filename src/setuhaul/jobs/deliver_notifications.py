"""Prototype delivery runner. Production uses an external scheduled worker with a real transport."""

# Lazy annotations.
from __future__ import annotations

# sqlite3 for the Connection type.
import sqlite3

# The injectable clock.
from setuhaul.clock import Clock

# The delivery engine and its types.
from setuhaul.notifications import DeliveryResult, Sender, deliver_pending_notifications


def run_delivery_once(conn: sqlite3.Connection, clock: Clock, sender: Sender, limit: int = 100) -> DeliveryResult:
    # A thin entry point for a scheduler: run one delivery pass and report the counts.
    return deliver_pending_notifications(conn, clock, sender, limit)
