"""Send and poll jobs: at-least-once delivery that never double-books."""

from __future__ import annotations

import sqlite3

import pytest

from setuhaul import partner_booking
from setuhaul.clock import FakeClock
from setuhaul.jobs.poll_partner_replies import poll_partner_replies
from setuhaul.jobs.send_partner_requests import send_pending_requests
from setuhaul.mail_transport import IncomingMail, InMemoryMailbox, MailDeliveryError, OutgoingMail
from setuhaul.partner_booking import request_slot
from tests.helpers import at, seed_partner_facility


@pytest.fixture()
def asked(db: sqlite3.Connection, clock: FakeClock) -> str:
    seed_partner_facility(db)
    ref = request_slot(db, clock, shipment_id="SHP1006", proposed_start=at("12:00"), proposed_end=at("13:00")).request_ref
    assert ref
    return ref


def _approval(ref: str, mid: str = "<r1@p>") -> IncomingMail:
    return IncomingMail(message_id=mid, from_addr="partner.desk@partner.example", subject=f"Re: SetuHaul slot request [{ref}]",
                        in_reply_to=None, body="APPROVE", authentication_results=None)


def test_send_pending_requests_sends_once_and_stores_message_id(db, clock, asked) -> None:
    box = InMemoryMailbox()
    assert send_pending_requests(db, clock, box) == 1
    assert send_pending_requests(db, clock, box) == 0
    assert len(box.sent) == 1
    row = db.execute("select email_status, message_id from partner_requests").fetchone()
    assert row["email_status"] == "SENT" and row["message_id"] == box.sent[0].message_id


class Failing:
    def send(self, mail: OutgoingMail) -> None:
        raise MailDeliveryError("SMTP boom")


def test_send_failure_counts_attempts_then_marks_failed_after_five(db, clock, asked) -> None:
    for _ in range(4):
        send_pending_requests(db, clock, Failing())
    row = db.execute("select email_status, email_attempts, last_error from partner_requests").fetchone()
    assert (row["email_status"], row["email_attempts"], row["last_error"]) == ("PENDING", 4, "SMTP boom")
    send_pending_requests(db, clock, Failing())
    assert db.execute("select email_status from partner_requests").fetchone()[0] == "FAILED"


def test_poll_books_on_valid_reply_and_marks_seen(db, clock, asked) -> None:
    box = InMemoryMailbox()
    box.inject(_approval(asked))
    assert poll_partner_replies(db, clock, box) == 1
    assert db.execute("select count(*) from appointments where booking_source='WAREHOUSE'").fetchone()[0] == 1
    assert box.fetch_unseen() == []


def test_poll_twice_same_mail_books_once(db, clock, asked) -> None:
    box = InMemoryMailbox()
    box.inject(_approval(asked))
    poll_partner_replies(db, clock, box)
    box.unsee("<r1@p>")  # the mail reappears as unseen
    poll_partner_replies(db, clock, box)
    assert db.execute("select count(*) from appointments where booking_source='WAREHOUSE'").fetchone()[0] == 1


def test_poll_crash_before_mark_seen_rereads_without_double_booking(db, clock, asked) -> None:
    class FlakyMark(InMemoryMailbox):
        failed = False

        def mark_seen(self, message_id: str) -> None:
            if not self.failed:
                self.failed = True
                raise OSError("imap dropped")
            super().mark_seen(message_id)
    box = FlakyMark()
    box.inject(_approval(asked))
    poll_partner_replies(db, clock, box)
    assert [m.message_id for m in box.fetch_unseen()] == ["<r1@p>"]  # still unseen
    poll_partner_replies(db, clock, box)
    assert box.fetch_unseen() == []
    assert db.execute("select count(*) from appointments where booking_source='WAREHOUSE'").fetchone()[0] == 1


def test_one_failing_mail_does_not_block_the_next(db, clock, asked, monkeypatch) -> None:
    real = partner_booking.handle_reply
    calls: list[str] = []

    def flaky(conn, clk, **kw):  # type: ignore[no-untyped-def]
        calls.append(kw["message_id"])
        if kw["message_id"] == "<bad@p>":
            raise RuntimeError("boom")
        return real(conn, clk, **kw)
    monkeypatch.setattr("setuhaul.jobs.poll_partner_replies.handle_reply", flaky)
    box = InMemoryMailbox()
    box.inject(_approval(asked, "<bad@p>"))
    box.inject(_approval(asked, "<good@p>"))
    assert poll_partner_replies(db, clock, box) == 1
    assert calls == ["<bad@p>", "<good@p>"]
    assert [m.message_id for m in box.fetch_unseen()] == ["<bad@p>"]
