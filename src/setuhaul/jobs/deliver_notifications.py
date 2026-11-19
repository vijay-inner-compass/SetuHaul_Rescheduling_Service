"""Prototype delivery runner. Production uses an external scheduled worker with a real transport."""

from __future__ import annotations

import sqlite3

from setuhaul.clock import Clock
from setuhaul.notifications import DeliveryResult, Sender, deliver_pending_notifications


def run_delivery_once(conn: sqlite3.Connection, clock: Clock, sender: Sender, limit: int = 100) -> DeliveryResult:
    return deliver_pending_notifications(conn, clock, sender, limit)
