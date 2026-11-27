"""Prototype delivery runner (tests and demo console only).

Do NOT wire this runner as-is into the deployed service: ``deliver_pending_notifications`` selects every
``recipient_type``, including ``DRIVER``, and would mark driver notices SENT without delivering them to Telegram.
Driver notices are delivered by ``jobs/send_driver_notifications.py``; planner, consignee and operations rows stay
PENDING in ``notification_outbox`` until a real outbound transport is connected for them.
"""

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
