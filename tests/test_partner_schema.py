"""Partner-request schema: one open request per shipment, partner flag defaults to off."""

from __future__ import annotations

import sqlite3

import pytest


def _insert(conn: sqlite3.Connection, ref: str, shipment: str, status: str) -> None:
    conn.execute(
        "insert into partner_requests (request_ref, shipment_id, facility_id, contact_id, proposed_start, proposed_end, "
        "status, email_status, email_attempts, created_at, expires_at) "
        "values (?,?,?,?,?,?,?,?,?,?,?)",
        (ref, shipment, "FAC-JAI-01", "CON001", "2026-08-04T12:00:00+05:30", "2026-08-04T13:00:00+05:30",
         status, "PENDING", 0, "2026-08-04T09:35:00+05:30", "2026-08-04T11:35:00+05:30"),
    )


def test_second_pending_request_for_same_shipment_is_rejected(db: sqlite3.Connection) -> None:
    _insert(db, "PR-00000001", "SHP1006", "PENDING_PARTNER")
    with pytest.raises(sqlite3.IntegrityError):
        _insert(db, "PR-00000002", "SHP1006", "PENDING_PARTNER")


def test_declined_plus_pending_for_same_shipment_is_allowed(db: sqlite3.Connection) -> None:
    _insert(db, "PR-00000001", "SHP1006", "DECLINED")
    _insert(db, "PR-00000002", "SHP1006", "PENDING_PARTNER")


def test_is_partner_defaults_to_zero(db: sqlite3.Connection) -> None:
    flags = {r[0] for r in db.execute("select is_partner from facilities")}
    assert flags == {0}
