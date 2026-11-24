"""Telegram binding by shared contact: who may bind, who is refused, and what is audited."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from setuhaul.auth import onboarding, telegram, users
from setuhaul.auth.users import User
from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection
from setuhaul.identity import AuthError

# DRV006's supplied phone is stored hyphenated as +91-9000010006.
DRV6 = "DRV006"


def _env(tmp_path: Path, clock: FakeClock):  # type: ignore[no-untyped-def]
    # A database with an admin and a warehouse authority.
    path = make_working_db(tmp_path / "t.sqlite3")
    conn = open_connection(path=path)
    fac = conn.execute("select facility_id from facilities order by facility_id limit 1").fetchone()[0]
    admin = users.bootstrap_admin(conn, "boss@x.com", clock)
    wh = users.invite(conn, actor=admin, email="wh@x.com", role="WAREHOUSE_AUTHORITY", display_name="W",
                      facilities=(fac,), clock=clock)
    return conn, admin, wh


_OWN = object()


def _bind(conn: sqlite3.Connection, clock: FakeClock, phone: str = "+91 90000 10006", user_id: int = 111,
          contact_user_id: object = _OWN, chat_id: int | None = None) -> telegram.BindResult:
    # By default the contact card belongs to the sender.
    contact = user_id if contact_user_id is _OWN else contact_user_id
    return telegram.bind_from_contact(conn, telegram_user_id=user_id, chat_id=chat_id or user_id,
                                      contact_user_id=contact, contact_phone=phone, clock=clock)  # type: ignore[arg-type]


def _onboard(conn: sqlite3.Connection, admin: User, wh: User, clock: FakeClock, approve: bool = True,
             days: int = 30, phone: str = "+91 90000 10099") -> str:
    veh = conn.execute("select vehicle_id, carrier_id from vehicles where active_flag=1 order by vehicle_id").fetchone()
    did = onboarding.add_driver(conn, actor=wh, name="Ravi", phone=phone, licence_number=f"LIC-{phone[-2:]}",
                                licence_expiry=clock.now().date() + timedelta(days=days), carrier_id=veh["carrier_id"],
                                vehicle_id=veh["vehicle_id"], clock=clock)
    if approve:
        onboarding.approve(conn, actor=admin, driver_id=did, clock=clock)
    return did


def _audits(conn: sqlite3.Connection, action: str) -> list[sqlite3.Row]:
    return list(conn.execute("select * from audit_log where action=? order by audit_id", (action,)))


def test_normalise_formats() -> None:
    from setuhaul.auth.phone import normalise_e164

    for raw in ("+91 90000 10006", "09000010006", "919000010006", "9000010006"):
        assert normalise_e164(raw) == "+919000010006"
    assert normalise_e164("abc") is None


@pytest.mark.parametrize("phone", ["+91 90000 10006", "09000010006", "919000010006", "9000010006", "+91-9000010006"])
def test_contact_of_sender_binds_approved_driver(tmp_path: Path, clock: FakeClock, phone: str) -> None:
    conn, *_ = _env(tmp_path, clock)
    # Every phone format binds the same supplied driver, whose stored number is hyphenated.
    assert _bind(conn, clock, phone=phone) == telegram.BindResult.BOUND
    row = conn.execute("select * from telegram_identities").fetchone()
    assert row["driver_id"] == DRV6 and row["telegram_user_id"] == "111" and row["chat_id"] == "111"
    assert row["revoked_at"] is None
    # The bound sender now resolves to the driver.
    assert telegram.resolve_driver(conn, 111, clock.now().date()) == DRV6
    # An unbound sender resolves to nobody.
    assert telegram.resolve_driver(conn, 222, clock.now().date()) is None


def test_forwarded_contact_with_other_user_id_refused(tmp_path: Path, clock: FakeClock) -> None:
    conn, *_ = _env(tmp_path, clock)
    # Someone forwards a driver's contact card: the contact's user id is not the sender's.
    assert _bind(conn, clock, user_id=111, contact_user_id=999) == telegram.BindResult.REFUSED
    assert conn.execute("select count(*) from telegram_identities").fetchone()[0] == 0


def test_contact_without_user_id_refused(tmp_path: Path, clock: FakeClock) -> None:
    conn, *_ = _env(tmp_path, clock)
    assert _bind(conn, clock, contact_user_id=None) == telegram.BindResult.REFUSED
    assert conn.execute("select count(*) from telegram_identities").fetchone()[0] == 0


def test_pending_or_rejected_driver_refused(tmp_path: Path, clock: FakeClock) -> None:
    conn, admin, wh = _env(tmp_path, clock)
    pending = _onboard(conn, admin, wh, clock, approve=False, phone="+91 90000 10098")
    rejected = _onboard(conn, admin, wh, clock, approve=False, phone="+91 90000 10097")
    onboarding.reject(conn, actor=admin, driver_id=rejected, clock=clock)
    assert pending != rejected
    assert _bind(conn, clock, phone="+91 90000 10098", user_id=1) == telegram.BindResult.REFUSED
    assert _bind(conn, clock, phone="+91 90000 10097", user_id=2) == telegram.BindResult.REFUSED
    assert conn.execute("select count(*) from telegram_identities").fetchone()[0] == 0


def test_suspended_driver_refused(tmp_path: Path, clock: FakeClock) -> None:
    conn, *_ = _env(tmp_path, clock)
    conn.execute("update drivers set driver_status='SUSPENDED' where driver_id=?", (DRV6,))
    conn.commit()
    assert _bind(conn, clock) == telegram.BindResult.REFUSED


def test_expired_licence_refused(tmp_path: Path, clock: FakeClock) -> None:
    conn, admin, wh = _env(tmp_path, clock)
    _onboard(conn, admin, wh, clock, days=5)
    clock.advance(timedelta(days=6))
    assert _bind(conn, clock, phone="+91 90000 10099") == telegram.BindResult.REFUSED
    assert conn.execute("select count(*) from telegram_identities").fetchone()[0] == 0


def test_approved_onboarded_driver_binds(tmp_path: Path, clock: FakeClock) -> None:
    conn, admin, wh = _env(tmp_path, clock)
    did = _onboard(conn, admin, wh, clock)
    assert _bind(conn, clock, phone="9000010099") == telegram.BindResult.BOUND
    assert telegram.resolve_driver(conn, 111, clock.now().date()) == did


def test_unregistered_phone_gets_same_reply_as_wrong_user(tmp_path: Path, clock: FakeClock) -> None:
    conn, *_ = _env(tmp_path, clock)
    unknown = _bind(conn, clock, phone="+91 80000 00000")
    wrong_user = _bind(conn, clock, contact_user_id=5)
    # The same result, so the caller can send the same text and learn nothing about the registry.
    assert unknown == wrong_user == telegram.BindResult.REFUSED
    # The reasons differ, but only in the audit detail.
    reasons = [json.loads(r["detail_json"])["reason"] for r in _audits(conn, "TELEGRAM_BIND_REFUSED")]
    assert len(set(reasons)) == 2


def test_second_telegram_account_for_same_driver_refused(tmp_path: Path, clock: FakeClock) -> None:
    conn, *_ = _env(tmp_path, clock)
    assert _bind(conn, clock, user_id=111) == telegram.BindResult.BOUND
    # A different Telegram account sharing the same driver's number is refused; the first stays bound.
    assert _bind(conn, clock, user_id=222) == telegram.BindResult.REFUSED
    assert telegram.resolve_driver(conn, 111, clock.now().date()) == DRV6
    assert telegram.resolve_driver(conn, 222, clock.now().date()) is None


def test_one_telegram_account_cannot_bind_two_drivers(tmp_path: Path, clock: FakeClock) -> None:
    conn, *_ = _env(tmp_path, clock)
    assert _bind(conn, clock, phone="+91 90000 10006") == telegram.BindResult.BOUND
    assert _bind(conn, clock, phone="+91 90000 10007") == telegram.BindResult.REFUSED
    assert conn.execute("select count(*) from telegram_identities").fetchone()[0] == 1


def test_revoked_binding_refused_and_rebind_allowed_only_when_approved(tmp_path: Path, clock: FakeClock) -> None:
    conn, admin, wh = _env(tmp_path, clock)
    assert _bind(conn, clock, user_id=111) == telegram.BindResult.BOUND
    telegram.revoke(conn, actor=wh, driver_id=DRV6, clock=clock)
    # A revoked binding no longer resolves.
    assert telegram.resolve_driver(conn, 111, clock.now().date()) is None
    # While the driver is suspended, re-binding is refused.
    conn.execute("update drivers set driver_status='SUSPENDED' where driver_id=?", (DRV6,))
    conn.commit()
    assert _bind(conn, clock, user_id=333) == telegram.BindResult.REFUSED
    conn.execute("update drivers set driver_status='ACTIVE' where driver_id=?", (DRV6,))
    conn.commit()
    # Once eligible again a new account (or the same one) binds by reusing the revoked row.
    assert _bind(conn, clock, user_id=333) == telegram.BindResult.BOUND
    assert telegram.resolve_driver(conn, 333, clock.now().date()) == DRV6
    assert telegram.resolve_driver(conn, 111, clock.now().date()) is None
    rows = conn.execute("select * from telegram_identities").fetchall()
    assert len(rows) == 1 and rows[0]["revoked_at"] is None
    # The old account may bind again later too (its row is not in the way).
    telegram.revoke(conn, actor=admin, driver_id=DRV6, clock=clock)
    assert _bind(conn, clock, user_id=111) == telegram.BindResult.BOUND
    assert len(conn.execute("select * from telegram_identities").fetchall()) == 1


def test_revoke_needs_an_active_actor_and_a_binding(tmp_path: Path, clock: FakeClock) -> None:
    conn, admin, wh = _env(tmp_path, clock)
    _bind(conn, clock)
    # A disabled actor is refused, and the binding stays.
    with pytest.raises(AuthError) as exc:
        telegram.revoke(conn, actor=wh.model_copy(update={"status": "DISABLED"}), driver_id=DRV6, clock=clock)
    assert exc.value.code == "FORBIDDEN"
    assert telegram.resolve_driver(conn, 111, clock.now().date()) == DRV6
    # Revoking a driver with no binding says so.
    with pytest.raises(AuthError) as exc2:
        telegram.revoke(conn, actor=admin, driver_id="DRV009", clock=clock)
    assert exc2.value.code == "NOT_BOUND"


def test_licence_expiry_stops_resolution(tmp_path: Path, clock: FakeClock) -> None:
    conn, admin, wh = _env(tmp_path, clock)
    did = _onboard(conn, admin, wh, clock, days=5)
    assert _bind(conn, clock, phone="+91 90000 10099") == telegram.BindResult.BOUND
    assert telegram.resolve_driver(conn, 111, clock.now().date()) == did
    clock.advance(timedelta(days=6))
    assert telegram.resolve_driver(conn, 111, clock.now().date()) is None


def test_bind_and_refusals_audited(tmp_path: Path, clock: FakeClock) -> None:
    conn, _, wh = _env(tmp_path, clock)
    _bind(conn, clock, contact_user_id=None)
    _bind(conn, clock)
    telegram.revoke(conn, actor=wh, driver_id=DRV6, clock=clock)
    refused, ok, revoked = (_audits(conn, a) for a in ("TELEGRAM_BIND_REFUSED", "TELEGRAM_BIND_OK", "TELEGRAM_REVOKED"))
    assert len(refused) == 1 and refused[0]["outcome"] == "REFUSED"
    assert json.loads(refused[0]["detail_json"])["reason"] == "CONTACT_NOT_OWN"
    assert len(ok) == 1 and ok[0]["target_id"] == DRV6 and ok[0]["outcome"] == "OK"
    assert len(revoked) == 1 and revoked[0]["actor_user_id"] == wh.user_id and revoked[0]["target_id"] == DRV6
    # No phone number is written to the audit detail.
    assert "9000010006" not in "".join(r["detail_json"] or "" for r in conn.execute("select detail_json from audit_log"))
