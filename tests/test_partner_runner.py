"""Background partner-mail runner: automatic send, reply polling, expiry, isolation and restart safety."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import timedelta
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

from setuhaul.api import AppDeps, create_app, partner_mail_from_env
from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from setuhaul.events import queue_driver_notice
from setuhaul.jobs.partner_mail import PartnerMailRunner
from setuhaul.jobs.send_partner_requests import send_pending_requests
from setuhaul.mail_transport import (
    ImapReader,
    IncomingMail,
    InMemoryMailbox,
    MailDeliveryError,
    OutgoingMail,
    SmtpSender,
)
from setuhaul.partner_booking import request_slot
from tests.helpers import at, seed_partner_facility


def _wait_until(predicate, timeout: float = 3.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate(), "condition was not met in time"


def _approval(ref: str, mid: str = "<r1@p>", body: str = "APPROVE") -> IncomingMail:
    return IncomingMail(
        message_id=mid,
        from_addr="partner.desk@partner.example",
        subject=f"Re: SetuHaul slot request [{ref}]",
        in_reply_to=None,
        body=body,
        authentication_results=None,
    )


def _deps(db_path: Path, clock: FakeClock, box: InMemoryMailbox, **extra: object) -> AppDeps:
    return AppDeps(
        db_path=db_path,
        redis=fakeredis.FakeRedis(decode_responses=True),
        webhook_secret=b"test-secret",
        coordinator_tokens={},
        fallback_path=db_path.parent / "fallback.jsonl",
        clock=clock,
        partner_mail=(box, box),
        run_partner_mail=True,
        partner_mail_interval_s=0.05,
        **extra,  # type: ignore[arg-type]
    )


def _driver_notices(db_path: Path, shipment_id: str) -> list[sqlite3.Row]:
    conn = open_connection(path=db_path, readonly=True)
    try:
        return conn.execute(
            "select o.* from notification_outbox o left join appointment_events e on e.event_id=o.event_id "
            "where o.recipient_type='DRIVER' and coalesce(e.shipment_id, o.shipment_id)=? order by o.outbox_id",
            (shipment_id,),
        ).fetchall()
    finally:
        conn.close()


def test_partner_runner_sends_and_processes_approve_automatically(working_db_path: Path, clock: FakeClock) -> None:
    db_path = working_db_path
    conn = open_connection(path=db_path)
    try:
        seed_partner_facility(conn)
        ref = request_slot(conn, clock, shipment_id="SHP1006", proposed_start=at("12:00"), proposed_end=at("13:00")).request_ref
        assert ref
    finally:
        conn.close()

    box = InMemoryMailbox()
    with TestClient(create_app(_deps(db_path, clock, box))):
        _wait_until(lambda: len(box.sent) == 1)
        box.inject(_approval(ref, "<approve-1@p>", "APPROVE"))

        def approved() -> bool:
            c = open_connection(path=db_path, readonly=True)
            try:
                r = c.execute("select status from partner_requests where request_ref=?", (ref,)).fetchone()
                return r is not None and r["status"] == "APPROVED"
            finally:
                c.close()

        _wait_until(approved)

    check = open_connection(path=db_path, readonly=True)
    try:
        assert check.execute("select count(*) from appointments where booking_source='WAREHOUSE'").fetchone()[0] == 1
        assert len(_driver_notices(db_path, "SHP1006")) == 1
    finally:
        check.close()


def test_partner_runner_sends_and_processes_decline_automatically(working_db_path: Path, clock: FakeClock) -> None:
    db_path = working_db_path
    conn = open_connection(path=db_path)
    try:
        seed_partner_facility(conn)
        ref = request_slot(conn, clock, shipment_id="SHP1006", proposed_start=at("12:00"), proposed_end=at("13:00")).request_ref
        assert ref
    finally:
        conn.close()

    box = InMemoryMailbox()
    with TestClient(create_app(_deps(db_path, clock, box))):
        _wait_until(lambda: len(box.sent) == 1)
        box.inject(_approval(ref, "<decline-1@p>", "DECLINE"))

        def declined() -> bool:
            c = open_connection(path=db_path, readonly=True)
            try:
                r = c.execute("select status from partner_requests where request_ref=?", (ref,)).fetchone()
                return r is not None and r["status"] == "DECLINED"
            finally:
                c.close()

        _wait_until(declined)

    check = open_connection(path=db_path, readonly=True)
    try:
        assert check.execute("select count(*) from appointments where booking_source='WAREHOUSE'").fetchone()[0] == 0
        assert len(_driver_notices(db_path, "SHP1006")) == 1
    finally:
        check.close()


def test_partner_runner_sends_and_processes_expiry_automatically(working_db_path: Path, clock: FakeClock) -> None:
    db_path = working_db_path
    conn = open_connection(path=db_path)
    try:
        seed_partner_facility(conn)
        ref = request_slot(conn, clock, shipment_id="SHP1006", proposed_start=at("12:00"), proposed_end=at("13:00")).request_ref
        assert ref
    finally:
        conn.close()

    box = InMemoryMailbox()
    with TestClient(create_app(_deps(db_path, clock, box))):
        _wait_until(lambda: len(box.sent) == 1)
        clock.advance(timedelta(minutes=121))

        def expired() -> bool:
            c = open_connection(path=db_path, readonly=True)
            try:
                r = c.execute("select status from partner_requests where request_ref=?", (ref,)).fetchone()
                return r is not None and r["status"] == "EXPIRED"
            finally:
                c.close()

        _wait_until(expired)

    assert len(_driver_notices(db_path, "SHP1006")) == 1


def test_partner_runner_isolates_step_failures(db: sqlite3.Connection, clock: FakeClock) -> None:
    seed_partner_facility(db)
    ref = request_slot(db, clock, shipment_id="SHP1006", proposed_start=at("12:00"), proposed_end=at("13:00")).request_ref
    assert ref
    clock.advance(timedelta(minutes=121))

    class BrokenMail:
        def send(self, mail: OutgoingMail) -> None:
            raise RuntimeError("smtp crash")

        def fetch_unseen(self) -> list[IncomingMail]:
            raise MailDeliveryError("IMAP OSError")

        def mark_seen(self, message_id: str) -> None:
            return None

        def close(self) -> None:
            return None

    broken = BrokenMail()
    result = PartnerMailRunner(db, clock, broken, broken).run_once()
    assert result.sent == 0
    assert result.replies_handled == 0
    assert result.expired == 1
    assert db.execute("select status from partner_requests where request_ref=?", (ref,)).fetchone()[0] == "EXPIRED"


def test_slow_mail_server_does_not_delay_hold_expiry_or_driver_notices(working_db_path: Path, clock: FakeClock) -> None:
    db_path = working_db_path
    conn = open_connection(path=db_path)
    try:
        seed_partner_facility(conn)
        request_slot(conn, clock, shipment_id="SHP1006", proposed_start=at("12:00"), proposed_end=at("13:00"))
        drv6 = conn.execute("select driver_id from shipments where shipment_id='SHP1006'").fetchone()[0]
        drv7 = conn.execute("select driver_id from shipments where shipment_id='SHP1007'").fetchone()[0]
        # Bind SHP1006's driver to Telegram and queue a driver notice.
        conn.execute(
            "insert into telegram_identities (driver_id, telegram_user_id, chat_id, bound_at) values (?, '101', '12345', ?)",
            (drv6, clock.now().isoformat()),
        )
        queue_driver_notice(conn, drv6, "SHP1006", "Test notice", clock.now())
        # Create an overdue HELD appointment on another shipment (SHP1007) for MaintenanceRunner to expire.
        slot_row = conn.execute(
            "select slot_id, facility_id from appointment_slots where slot_status='OPEN' "
            "and slot_id not in (select slot_id from appointments where appointment_status in ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS','HELD')) "
            "order by slot_start_ts limit 1"
        ).fetchone()
        slot_id, fac_id = slot_row["slot_id"], slot_row["facility_id"]
        hold_id = "APT-HOLD-TEST"
        held_at = (clock.now() - timedelta(minutes=5)).isoformat()
        exp_dt = clock.now() - timedelta(minutes=1)
        conn.execute(
            "insert into chat_threads (thread_id, driver_id, shipment_id, opened_at, thread_status, thread_intent) "
            "values ('THR-T', ?, 'SHP1007', ?, 'OPEN', 'REPORT_DELAY')",
            (drv7, held_at),
        )
        conn.execute(
            "insert into option_sets (option_set_id, driver_id, thread_id, shipment_id, facility_id, "
            "shown_at, expires_at, rules_version, context_json) values ('OPT-T', ?, 'THR-T', 'SHP1007', ?, ?, ?, 'v1', '{}')",
            (drv7, fac_id, held_at, exp_dt.isoformat()),
        )
        conn.execute(
            "insert into appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source, "
            "is_current, booked_at, updated_at) values (?, 'SHP1007', ?, 'HELD', 'DRIVER_CHAT', 0, ?, ?)",
            (hold_id, slot_id, held_at, held_at),
        )
        conn.execute(
            "insert into appointment_hold_meta (appointment_id, driver_id, thread_id, option_set_id, "
            "expires_at, expires_epoch, request_fingerprint) values (?, ?, 'THR-T', 'OPT-T', ?, ?, 'fp')",
            (hold_id, drv7, exp_dt.isoformat(), int(exp_dt.timestamp())),
        )
        conn.commit()
    finally:
        conn.close()

    unblock = threading.Event()
    mail_started = threading.Event()

    class HungMail:
        def send(self, mail: OutgoingMail) -> None:
            mail_started.set()
            unblock.wait(timeout=10)

        def fetch_unseen(self) -> list[IncomingMail]:
            mail_started.set()
            unblock.wait(timeout=10)
            return []

        def mark_seen(self, message_id: str) -> None:
            return None

        def close(self) -> None:
            return None

    sent_tg: list[tuple[str, str]] = []

    class FakeTg:
        def send(self, chat_id: int, text: str) -> None:
            sent_tg.append((str(chat_id), text))

    hung = HungMail()
    deps = AppDeps(
        db_path=db_path,
        redis=fakeredis.FakeRedis(decode_responses=True),
        webhook_secret=b"test-secret",
        coordinator_tokens={},
        fallback_path=db_path.parent / "fallback.jsonl",
        clock=clock,
        run_maintenance=True,
        maintenance_interval_s=0.05,
        telegram=FakeTg(),
        run_driver_notices=True,
        notice_interval_s=0.05,
        partner_mail=(hung, hung),
        run_partner_mail=True,
        partner_mail_interval_s=0.05,
    )
    try:
        with TestClient(create_app(deps)):
            assert mail_started.wait(timeout=2.0), "partner mail runner did not start"

            def hold_expired() -> bool:
                c = open_connection(path=db_path, readonly=True)
                try:
                    row = c.execute(
                        "select appointment_status, cancellation_reason from appointments where appointment_id=?",
                        (hold_id,),
                    ).fetchone()
                    return (
                        row is not None
                        and row["appointment_status"] == "CANCELLED"
                        and row["cancellation_reason"] == "HOLD_EXPIRED"
                    )
                finally:
                    c.close()

            _wait_until(hold_expired, timeout=2.0)
            _wait_until(lambda: len(sent_tg) == 1, timeout=2.0)
            # Partner mail is still blocked while both hold expiry and driver notices completed.
            assert not unblock.is_set()
            unblock.set()
    finally:
        unblock.set()


def test_restart_and_at_least_once_send_never_double_books(db: sqlite3.Connection, clock: FakeClock) -> None:
    seed_partner_facility(db)
    ref = request_slot(db, clock, shipment_id="SHP1006", proposed_start=at("12:00"), proposed_end=at("13:00")).request_ref
    assert ref

    box = InMemoryMailbox()

    class CrashAfterSend:
        def send(self, mail: OutgoingMail) -> None:
            # Email went out on the wire, then the process crashed before marking SENT.
            box.send(mail)
            raise MailDeliveryError("connection reset after DATA")

    # First run: sends email, fails to mark SENT.
    PartnerMailRunner(db, clock, CrashAfterSend(), box).run_once()
    assert len(box.sent) == 1
    assert db.execute("select email_status from partner_requests where request_ref=?", (ref,)).fetchone()[0] == "PENDING"

    # Restart with healthy sender: resends with the exact same message_id.
    PartnerMailRunner(db, clock, box, box).run_once()
    assert len(box.sent) == 2
    assert box.sent[0].message_id == box.sent[1].message_id
    assert db.execute("select email_status from partner_requests where request_ref=?", (ref,)).fetchone()[0] == "SENT"

    # Partner replies APPROVE to both emails (two different Message-IDs).
    box.inject(_approval(ref, "<reply-a@p>", "APPROVE"))
    box.inject(_approval(ref, "<reply-b@p>", "APPROVE"))
    PartnerMailRunner(db, clock, box, box).run_once()

    assert db.execute("select count(*) from appointments where booking_source='WAREHOUSE'").fetchone()[0] == 1
    assert db.execute(
        "select count(*) from notification_outbox o left join appointment_events e on e.event_id=o.event_id "
        "where o.recipient_type='DRIVER' and coalesce(e.shipment_id, o.shipment_id)='SHP1006'"
    ).fetchone()[0] == 1
    outcomes = {
        r["message_id"]: r["outcome"]
        for r in db.execute("select message_id, outcome from partner_inbound").fetchall()
    }
    assert outcomes == {"<reply-a@p>": "BOOKED", "<reply-b@p>": "IGNORED_DUPLICATE"}


def test_send_pending_requests_skips_already_expired_request(db: sqlite3.Connection, clock: FakeClock) -> None:
    seed_partner_facility(db)
    ref = request_slot(db, clock, shipment_id="SHP1006", proposed_start=at("12:00"), proposed_end=at("13:00")).request_ref
    assert ref
    clock.advance(timedelta(minutes=121))
    box = InMemoryMailbox()
    assert send_pending_requests(db, clock, box) == 0
    assert box.sent == []


def test_partner_mail_from_env_only_enables_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETUHAUL_ENV_FILE", "")
    monkeypatch.delenv("SETUHAUL_PARTNER_EMAIL_MODE", raising=False)
    mail, require_auth, serv_id = partner_mail_from_env()
    assert mail is None and require_auth is False

    monkeypatch.setenv("SETUHAUL_PARTNER_EMAIL_MODE", "simulated")
    mail, require_auth, serv_id = partner_mail_from_env()
    assert mail is None and require_auth is False

    monkeypatch.setenv("SETUHAUL_PARTNER_EMAIL_MODE", "live")
    monkeypatch.setenv("SETUHAUL_SMTP_USER", "u@test")
    monkeypatch.setenv("SETUHAUL_SMTP_PASSWORD", "pw")
    monkeypatch.setenv("SETUHAUL_IMAP_USER", "u@test")
    monkeypatch.setenv("SETUHAUL_IMAP_PASSWORD", "pw")
    monkeypatch.setenv("SETUHAUL_AUTH_SERV_ID", "mx.example.test")
    mail, require_auth, serv_id = partner_mail_from_env()
    assert mail is not None
    assert isinstance(mail[0], SmtpSender) and isinstance(mail[1], ImapReader)
    assert require_auth is True and serv_id == "mx.example.test"

    # Missing required secret in live mode: fails closed (disabled) rather than crashing boot.
    monkeypatch.delenv("SETUHAUL_IMAP_PASSWORD")
    mail, require_auth, serv_id = partner_mail_from_env()
    assert mail is None and require_auth is False
