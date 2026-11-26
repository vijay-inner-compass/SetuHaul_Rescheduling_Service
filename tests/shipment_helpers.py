"""Shared set-up for the assigned-shipment tests."""

from __future__ import annotations

import sqlite3

ADMIN_ID = "USR-SHIPADMIN"


def mark_unconfirmed(conn: sqlite3.Connection, shipment_id: str, created_by: str = ADMIN_ID) -> None:
    """Give a supplied shipment a pending assignment, as the Shipments tab would for a new one."""
    conn.execute("insert or ignore into users(user_id, email, role, status, display_name, invited_by, created_at) "
                 "values (?,?,'ADMIN','ACTIVE','Ship Admin',NULL,'2026-08-04T06:00:00+05:30')", (ADMIN_ID, "shipadmin@x.com"))
    conn.execute("insert into shipment_assignments(shipment_id, created_by, created_at) values (?,?,'2026-08-04T06:00:00+05:30')",
                 (shipment_id, created_by))
    conn.commit()


def confirm_row(conn: sqlite3.Connection, shipment_id: str) -> None:
    conn.execute("update shipment_assignments set confirmed_at='2026-08-04T07:00:00+05:30' where shipment_id=?", (shipment_id,))
    conn.commit()
