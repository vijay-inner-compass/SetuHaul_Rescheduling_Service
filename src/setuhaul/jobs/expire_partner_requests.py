"""Scheduler entry point: expire partner requests that were not answered in time."""

# Lazy annotations.
from __future__ import annotations

# sqlite3 for the Connection type.
import sqlite3

# The injectable clock.
from setuhaul.clock import Clock

# The expiry logic.
from setuhaul.partner_booking import expire_partner_requests


def run_expiry_once(conn: sqlite3.Connection, clock: Clock) -> int:
    # One pass; returns how many requests expired.
    return expire_partner_requests(conn, clock)
