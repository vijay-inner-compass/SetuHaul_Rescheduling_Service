"""Creating and expiring partner slot requests."""

from __future__ import annotations

import re
import sqlite3
from datetime import timedelta

from setuhaul.clock import FakeClock
from setuhaul.partner_booking import expire_partner_requests, render_request_email, request_slot
from tests.helpers import at, seed_partner_facility


def _ask(db: sqlite3.Connection, clock: FakeClock, shipment: str = "SHP1006"):
    return request_slot(db, clock, shipment_id=shipment, proposed_start=at("12:00"), proposed_end=at("13:00"))


def test_request_creates_one_pending_row_and_picks_active_contact(db: sqlite3.Connection, clock: FakeClock) -> None:
    seed_partner_facility(db)
    r = _ask(db, clock)
    assert r.status == "REQUESTED"
    assert re.fullmatch(r"PR-[0-9A-F]{8}", r.request_ref or "")
    row = db.execute("select * from partner_requests").fetchone()
    assert (row["status"], row["email_status"], row["contact_id"]) == ("PENDING_PARTNER", "PENDING", "CON-PARTNER-1")
    assert row["expires_at"] == (clock.now() + timedelta(minutes=120)).isoformat()


def test_second_request_same_shipment_returns_already_pending(db: sqlite3.Connection, clock: FakeClock) -> None:
    seed_partner_facility(db)
    first = _ask(db, clock)
    second = _ask(db, clock)
    assert second.status == "ALREADY_PENDING"
    assert second.request_ref == first.request_ref
    assert db.execute("select count(*) from partner_requests").fetchone()[0] == 1


def test_non_partner_facility_returns_not_partner_facility(db: sqlite3.Connection, clock: FakeClock) -> None:
    assert _ask(db, clock).status == "NOT_PARTNER_FACILITY"
    assert db.execute("select count(*) from partner_requests").fetchone()[0] == 0


def test_facility_without_active_contact_returns_no_partner_contact(db: sqlite3.Connection, clock: FakeClock) -> None:
    seed_partner_facility(db)
    db.execute("update facility_contacts set active_flag=0 where facility_id='FAC-PARTNER-01'")
    assert _ask(db, clock).status == "NO_PARTNER_CONTACT"


def test_unknown_or_cancelled_shipment_is_not_reschedulable(db: sqlite3.Connection, clock: FakeClock) -> None:
    seed_partner_facility(db)
    assert _ask(db, clock, "SHP-NOPE").status == "SHIPMENT_NOT_RESCHEDULABLE"
    db.execute("update shipments set current_status='CANCELLED' where shipment_id='SHP1006'")
    assert _ask(db, clock).status == "SHIPMENT_NOT_RESCHEDULABLE"


def test_expire_marks_overdue_pending_as_expired_and_leaves_original_untouched(db: sqlite3.Connection, clock: FakeClock) -> None:
    seed_partner_facility(db)
    _ask(db, clock)
    before = db.execute("select appointment_status, is_current from appointments where shipment_id='SHP1006'").fetchall()
    assert expire_partner_requests(db, clock) == 0
    clock.advance(timedelta(minutes=121))
    assert expire_partner_requests(db, clock) == 1
    assert db.execute("select status from partner_requests").fetchone()[0] == "EXPIRED"
    assert db.execute("select appointment_status, is_current from appointments where shipment_id='SHP1006'").fetchall() == before


def test_email_subject_has_ref_and_body_has_no_driver_phone(db: sqlite3.Connection, clock: FakeClock) -> None:
    seed_partner_facility(db)
    ref = _ask(db, clock).request_ref
    to_addr, subject, body = render_request_email(db, ref or "")
    assert to_addr == "partner.desk@partner.example"
    assert subject == f"SetuHaul slot request [{ref}]"
    assert "ORD-260804-006" in body and "APPROVE" in body and "DECLINE" in body
    phone = db.execute("select phone from drivers where driver_id='DRV006'").fetchone()[0]
    assert phone and phone not in body
