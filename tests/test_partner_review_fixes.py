"""Fixes from the whole-branch review: each test failed before its fix."""

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
    ref = request_slot(db, clock, shipment_id="SHP1006", proposed_start=at("12:00"), proposed_end=at("13:00")).request_ref
    assert ref
    return ref


def _reply(db, clock, ref, body="APPROVE", *, mid="<m1@x>", auth=None, require_auth=False, from_addr=CONTACT):  # type: ignore[no-untyped-def]
    return handle_reply(db, clock, message_id=mid, from_addr=from_addr, subject=f"Re: [{ref}]", in_reply_to=None, body=body,
                        authentication_results=auth, require_auth=require_auth)


def _warehouse_appts(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute("select * from appointments where booking_source='WAREHOUSE' order by appointment_id").fetchall()


# ---- C2: only a bare APPROVE / DECLINE on the first line counts ------------------------------------------------
@pytest.mark.parametrize("text", [
    "We can’t approve this slot", "We are not in a position to approve", "Approve nahi kar sakte", "nahi, approve nahi hoga",
    "Approved? Unfortunately we are full that day.", "I approve only if you come at 11", "We cannot do 10am but can approve 2pm",
    "Will check and revert.\n--\nRavi\nFSSAI approved warehouse", "Requests will be approved on my return",
    "Hi Neha,\nAPPROVE", "Approve, but later",
])
def test_conditional_or_refusing_replies_are_ambiguous(text: str) -> None:
    assert parse_decision(text) == "AMBIGUOUS"


@pytest.mark.parametrize("text,expected", [
    ("APPROVE", "APPROVE"), ("  approved.  ", "APPROVE"), ("Approve, thanks", "APPROVE"), ("APPROVE\n\n--\nRavi", "APPROVE"),
    ("\n\nDECLINE", "DECLINE"), ("Declined - sorry", "AMBIGUOUS"), ("Declined, thanks", "DECLINE"),
])
def test_bare_decisions_still_work(text: str, expected: str) -> None:
    assert parse_decision(text) == expected


# ---- I1: re-check the request inside the write transaction -----------------------------------------------------
def test_stale_second_approval_does_not_cancel_the_new_booking(db, clock, asked) -> None:  # type: ignore[no-untyped-def]
    stale = db.execute("select * from partner_requests where request_ref=?", (asked,)).fetchone()
    assert _reply(db, clock, asked, mid="<a>").status == "BOOKED"
    out = partner_booking._book(db, clock, stale, "<b>", CONTACT)  # a second worker still holding the old pending row
    assert out.status == "IGNORED_DUPLICATE"
    assert db.execute("select status from partner_requests").fetchone()[0] == "APPROVED"
    assert [r["appointment_status"] for r in _warehouse_appts(db)] == ["CONFIRMED"]


def test_request_expired_between_check_and_booking_does_not_book(db, clock, asked) -> None:  # type: ignore[no-untyped-def]
    stale = db.execute("select * from partner_requests where request_ref=?", (asked,)).fetchone()
    clock.advance(timedelta(minutes=121))
    partner_booking.expire_partner_requests(db, clock)
    assert partner_booking._book(db, clock, stale, "<b>", CONTACT).status == "ESCALATED"
    assert _warehouse_appts(db) == []
    assert db.execute("select is_current from appointments where appointment_id='APT1006'").fetchone()[0] == 1


# ---- I2 -------------------------------------------------------------------------------------------------------
def test_cancelled_shipment_is_not_booked_on_a_late_approval(db, clock, asked) -> None:  # type: ignore[no-untyped-def]
    db.execute("update shipments set current_status='CANCELLED' where shipment_id='SHP1006'")
    db.commit()
    assert _reply(db, clock, asked).status == "ESCALATED"
    assert _warehouse_appts(db) == []
    assert db.execute("select status from partner_requests").fetchone()[0] == "FAILED"


# ---- I3: a real capacity check on the partner's docks ----------------------------------------------------------
def _second_load(db: sqlite3.Connection, clock: FakeClock, start="12:00", end="13:00") -> str:  # type: ignore[no-untyped-def]
    db.execute("update shipments set destination_facility_id='FAC-PARTNER-01' where shipment_id='SHP1012'")
    db.commit()
    ref = request_slot(db, clock, shipment_id="SHP1012", proposed_start=at(start), proposed_end=at(end)).request_ref
    assert ref
    return ref


def test_second_truck_in_the_same_window_uses_another_dock(db, clock, asked) -> None:  # type: ignore[no-untyped-def]
    db.execute("insert into docks (dock_id, facility_id, dock_code, dock_type, supports_refrigerated, max_vehicle_weight_kg, dock_status) "
               "values ('DOCK-PARTNER-D2','FAC-PARTNER-01','D2','STANDARD',0,20000,'ACTIVE')")
    db.commit()
    second = _second_load(db, clock)
    assert _reply(db, clock, asked, mid="<a>").status == "BOOKED"
    assert _reply(db, clock, second, mid="<b>").status == "BOOKED"
    docks = {r[0] for r in db.execute("select sl.dock_id from appointments a join appointment_slots sl using(slot_id) "
                                      "where a.booking_source='WAREHOUSE'")}
    assert docks == {"DOCK-PARTNER-D1", "DOCK-PARTNER-D2"}


def test_one_dock_busy_fails_cleanly_with_the_real_reason(db, clock, asked) -> None:  # type: ignore[no-untyped-def]
    second = _second_load(db, clock, "12:30", "13:30")  # overlaps the first window
    assert _reply(db, clock, asked, mid="<a>").status == "BOOKED"
    out = _reply(db, clock, second, mid="<b>")
    assert out.status == "ESCALATED" and out.reason == "NO_FREE_DOCK"
    assert db.execute("select count(*) from appointments where shipment_id='SHP1012' and booking_source='WAREHOUSE'").fetchone()[0] == 0
    assert db.execute("select status from partner_requests where request_ref=?", (second,)).fetchone()[0] == "FAILED"


def test_window_of_a_cancelled_partner_booking_can_be_booked_again(db, clock, asked) -> None:  # type: ignore[no-untyped-def]
    assert _reply(db, clock, asked, mid="<a>").status == "BOOKED"
    db.execute("update appointments set appointment_status='CANCELLED', is_current=0 where booking_source='WAREHOUSE'")
    db.commit()
    second = _second_load(db, clock)
    assert _reply(db, clock, second, mid="<b>").status == "BOOKED"


# ---- I4: live mode needs real authentication --------------------------------------------------------------------
@pytest.mark.parametrize("auth", [None, "mx.google.com; spf=softfail; dkim=none; dmarc=none", "mx.google.com; spf=pass",
                                  "evil.example; dmarc=pass", "mx.google.com; dkim=pass header.d=other.example"])
def test_live_mode_escalates_unauthenticated_replies(db, clock, asked, auth) -> None:  # type: ignore[no-untyped-def]
    assert _reply(db, clock, asked, auth=auth, require_auth=True).status == "ESCALATED"
    assert _warehouse_appts(db) == []


@pytest.mark.parametrize("auth", ["mx.google.com; dkim=pass header.i=@partner.example header.s=s1; spf=pass; dmarc=pass (p=NONE)",
                                  "mx.google.com; dkim=pass header.d=partner.example"])
def test_live_mode_books_authenticated_replies(db, clock, asked, auth) -> None:  # type: ignore[no-untyped-def]
    assert _reply(db, clock, asked, auth=auth, require_auth=True).status == "BOOKED"


# ---- I6: a different reply after the decision is not swallowed ---------------------------------------------------
def test_reply_that_reverses_a_decision_escalates(db, clock, asked) -> None:  # type: ignore[no-untyped-def]
    assert _reply(db, clock, asked, mid="<a>").status == "BOOKED"
    out = _reply(db, clock, asked, "DECLINE", mid="<b>")
    assert out.status == "ESCALATED" and out.reason == "REPLY_AFTER_DECISION"
    assert db.execute("select count(*) from escalation_cases").fetchone()[0] == 1


# ---- M1 -> Important: a start time that has already passed ------------------------------------------------------
def test_start_in_the_past_is_refused(db: sqlite3.Connection, clock: FakeClock) -> None:
    seed_partner_facility(db)
    r = request_slot(db, clock, shipment_id="SHP1006", proposed_start=at("09:00"), proposed_end=at("10:00"))
    assert r.status == "START_IN_THE_PAST"
    assert db.execute("select count(*) from partner_requests").fetchone()[0] == 0
