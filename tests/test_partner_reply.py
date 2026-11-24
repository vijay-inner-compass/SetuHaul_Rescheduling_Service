"""Partner reply handling: only a verified, unambiguous, timely approval books a slot."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from setuhaul import partner_booking
from setuhaul.clock import FakeClock
from setuhaul.partner_booking import handle_reply, parse_decision, request_slot
from tests.helpers import at, seed_partner_facility

CONTACT = "partner.desk@partner.example"


@pytest.fixture()
def asked(db: sqlite3.Connection, clock: FakeClock) -> str:
    seed_partner_facility(db)
    r = request_slot(db, clock, shipment_id="SHP1006", proposed_start=at("12:00"), proposed_end=at("13:00"))
    assert r.request_ref
    return r.request_ref


def _reply(db: sqlite3.Connection, clock: FakeClock, ref: str, body: str = "APPROVE", *, mid: str = "<m1@x>",
           from_addr: str = CONTACT, subject: str | None = None, auth: str | None = None):
    return handle_reply(db, clock, message_id=mid, from_addr=from_addr,
                        subject=subject if subject is not None else f"Re: SetuHaul slot request [{ref}]",
                        in_reply_to=None, body=body, authentication_results=auth)


def _cases(db: sqlite3.Connection) -> int:
    return db.execute("select count(*) from escalation_cases").fetchone()[0]


def _original_is_current(db: sqlite3.Connection) -> bool:
    return db.execute("select is_current, appointment_status from appointments where appointment_id='APT1006'"
                      ).fetchone()[:] == (1, "CONFIRMED")


@pytest.mark.parametrize("text,expected", [
    ("APPROVE", "APPROVE"),
    ("approved, thanks", "APPROVE"),
    ("Decline", "DECLINE"),
    ("not approved", "AMBIGUOUS"),
    ("Reply APPROVE or DECLINE", "AMBIGUOUS"),
    ("APPROVE\n> Reply APPROVE or DECLINE", "APPROVE"),
    ("APPROVE\n\nOn Tue, 4 Aug 2026, SetuHaul <x@y> wrote:\n> please DECLINE or APPROVE", "APPROVE"),
    ("ok", "AMBIGUOUS"),
    ("", "AMBIGUOUS"),
])
def test_parse_decision(text: str, expected: str) -> None:
    assert parse_decision(text) == expected


def test_approve_books_with_warehouse_source_and_ends_original_atomically(db, clock, asked) -> None:
    out = _reply(db, clock, asked)
    assert out.status == "BOOKED" and out.appointment_id
    new = db.execute("select * from appointments where appointment_id=?", (out.appointment_id,)).fetchone()
    assert (new["booking_source"], new["appointment_status"], new["is_current"]) == ("WAREHOUSE", "CONFIRMED", 1)
    assert new["warehouse_confirmation_ref"] == "<m1@x>"
    assert new["replaced_appointment_id"] == "APT1006"
    old = db.execute("select appointment_status, is_current, cancellation_reason from appointments "
                     "where appointment_id='APT1006'").fetchone()
    assert tuple(old) == ("CANCELLED", 0, "RESCHEDULED")
    slot = db.execute("select facility_id, slot_start_ts from appointment_slots where slot_id=?", (new["slot_id"],)).fetchone()
    assert slot["facility_id"] == "FAC-PARTNER-01"
    req = db.execute("select status, appointment_id from partner_requests where request_ref=?", (asked,)).fetchone()
    assert tuple(req) == ("APPROVED", out.appointment_id)
    assert db.execute("select count(*) from appointment_events where event_type='PARTNER_BOOKING_CONFIRMED'").fetchone()[0] == 1


def test_fault_after_original_ended_rolls_back_everything(db, clock, asked, monkeypatch) -> None:
    def boom(point: str) -> None:
        if point == "AFTER_END_ORIGINAL":
            raise RuntimeError("crash")
    monkeypatch.setattr(partner_booking, "fault_hook", boom)
    with pytest.raises(RuntimeError):
        _reply(db, clock, asked)
    assert _original_is_current(db)
    assert db.execute("select status from partner_requests").fetchone()[0] == "PENDING_PARTNER"
    assert db.execute("select count(*) from partner_inbound").fetchone()[0] == 0
    monkeypatch.setattr(partner_booking, "fault_hook", None)
    assert _reply(db, clock, asked).status == "BOOKED"  # a retry after the crash still works


def test_decline_marks_declined_and_keeps_original(db, clock, asked) -> None:
    assert _reply(db, clock, asked, "DECLINE").status == "DECLINED"
    assert db.execute("select status from partner_requests").fetchone()[0] == "DECLINED"
    assert _original_is_current(db)
    assert _cases(db) == 0


def test_same_message_twice_books_once(db, clock, asked) -> None:
    assert _reply(db, clock, asked).status == "BOOKED"
    assert _reply(db, clock, asked).status == "IGNORED_DUPLICATE"
    assert db.execute("select count(*) from appointments where booking_source='WAREHOUSE'").fetchone()[0] == 1


def test_second_approval_message_after_booking_is_ignored(db, clock, asked) -> None:
    assert _reply(db, clock, asked, mid="<a>").status == "BOOKED"
    assert _reply(db, clock, asked, mid="<b>").status == "IGNORED_DUPLICATE"
    assert db.execute("select count(*) from appointments where booking_source='WAREHOUSE'").fetchone()[0] == 1


def test_wrong_sender_escalates_and_does_not_book(db, clock, asked) -> None:
    out = _reply(db, clock, asked, from_addr="attacker@evil.example")
    assert out.status == "ESCALATED"
    assert _original_is_current(db) and _cases(db) == 1
    assert db.execute("select status from partner_requests").fetchone()[0] == "PENDING_PARTNER"


def test_sender_match_ignores_case_and_display_name(db, clock, asked) -> None:
    assert _reply(db, clock, asked, from_addr='"Partner Desk" <Partner.Desk@Partner.Example>').status == "BOOKED"


def test_valid_sender_but_other_requests_ref_does_not_book_this_request(db, clock, asked) -> None:
    out = _reply(db, clock, asked, subject="Re: SetuHaul slot request [PR-DEADBEEF]")
    assert out.status == "UNMATCHED"
    assert _original_is_current(db)
    assert db.execute("select status from partner_requests").fetchone()[0] == "PENDING_PARTNER"


def test_unknown_ref_is_recorded_and_not_booked(db, clock, asked) -> None:
    assert _reply(db, clock, asked, subject="hello").status == "UNMATCHED"
    assert db.execute("select outcome from partner_inbound").fetchone()[0] == "UNMATCHED"


def test_approval_after_expiry_does_not_book(db, clock, asked) -> None:
    clock.advance(timedelta(minutes=121))
    out = _reply(db, clock, asked)
    assert out.status == "ESCALATED"
    assert _original_is_current(db)
    assert db.execute("select status from partner_requests").fetchone()[0] == "EXPIRED"


def test_original_in_progress_escalates_without_booking(db, clock, asked) -> None:
    db.execute("update appointments set appointment_status='IN_PROGRESS' where appointment_id='APT1006'")
    db.commit()
    assert _reply(db, clock, asked).status == "ESCALATED"
    assert db.execute("select count(*) from appointments where booking_source='WAREHOUSE'").fetchone()[0] == 0
    assert db.execute("select status from partner_requests").fetchone()[0] == "FAILED"


def test_slot_already_taken_ends_request_failed_and_escalates(db, clock, asked) -> None:
    db.execute("insert into appointment_slots (slot_id, facility_id, dock_id, slot_start_ts, slot_end_ts, slot_status, created_at)"
               f" values ('PSLOT-{asked}','FAC-PARTNER-01','DOCK-PARTNER-D1','2026-08-04T12:00:00+05:30',"
               "'2026-08-04T13:00:00+05:30','OPEN','2026-08-04T09:00:00+05:30')")
    db.execute("insert into appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source, is_current, "
               f"booked_at, updated_at) values ('APT-OTHER','SHP1012','PSLOT-{asked}','CONFIRMED','PLANNER',0,"
               "'2026-08-04T09:00:00+05:30','2026-08-04T09:00:00+05:30')")
    db.commit()
    assert _reply(db, clock, asked).status == "ESCALATED"
    assert _original_is_current(db)
    assert db.execute("select status from partner_requests").fetchone()[0] == "FAILED"


@pytest.mark.parametrize("auth", ["mx.google.com; spf=fail smtp.mailfrom=x", "dkim=fail header.d=x", "dmarc=fail"])
def test_failed_authentication_escalates(db, clock, asked, auth: str) -> None:
    assert _reply(db, clock, asked, auth=auth).status == "ESCALATED"
    assert _original_is_current(db)


def test_passing_or_absent_authentication_results_are_accepted(db, clock, asked) -> None:
    assert _reply(db, clock, asked, auth="mx.google.com; spf=pass; dkim=pass; dmarc=pass").status == "BOOKED"


def test_ambiguous_reply_escalates_and_request_stays_open(db, clock, asked) -> None:
    assert _reply(db, clock, asked, "Reply APPROVE or DECLINE").status == "ESCALATED"
    assert db.execute("select status from partner_requests").fetchone()[0] == "PENDING_PARTNER"
    assert _reply(db, clock, asked, "APPROVE", mid="<m2@x>").status == "BOOKED"  # a clear follow-up still works
