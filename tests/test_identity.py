"""Phase 3: verified channel identity, server-created sessions and rate limits."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from datetime import timedelta

import fakeredis
import pytest

from setuhaul.clock import FakeClock
from setuhaul.identity import (
    AuthError,
    authenticate_session,
    authorize_shipment,
    create_session,
    normalize_phone,
    provision_identities,
    resolve_driver,
    verify_channel_request,
)
from setuhaul.rate_limit import RateLimits, check_rate_limit

SECRET = b"test-webhook-secret"


def sign(body: bytes, secret: bytes = SECRET) -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


# --- phone normalisation ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+91-9000010001", "+919000010001"),
        ("+91 90000 10001", "+919000010001"),
        ("9000010001", "+919000010001"),
        ("919000010001", "+919000010001"),
        ("0091-9000010001", "+919000010001"),
    ],
)
def test_normalize_phone_to_e164(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "123", "+1", "90000100"])
def test_normalize_phone_rejects_garbage(raw: str) -> None:
    with pytest.raises(AuthError) as err:
        normalize_phone(raw)
    assert err.value.code == "INVALID_PHONE"


# --- webhook signature -----------------------------------------------------------------------
def test_valid_signature_accepted() -> None:
    body = b'{"from":"+919000010001","text":"late"}'
    assert verify_channel_request(body, sign(body), SECRET) is True


def test_invalid_signature_rejected_before_parsing() -> None:
    body = b'{"from":"+919000010001"}'
    with pytest.raises(AuthError) as err:
        verify_channel_request(body, sign(body, b"other-secret"), SECRET)
    assert err.value.code == "INVALID_SIGNATURE"


def test_tampered_body_and_missing_signature_rejected() -> None:
    body = b"hello"
    with pytest.raises(AuthError):
        verify_channel_request(b"hello!", sign(body), SECRET)
    with pytest.raises(AuthError):
        verify_channel_request(body, "", SECRET)


# --- phone -> driver ---------------------------------------------------------------------------
def test_provision_and_resolve_known_driver(db: sqlite3.Connection) -> None:
    assert provision_identities(db) == 15
    ctx = resolve_driver(db, "+91 9000010006")
    assert ctx.driver_id == "DRV006"
    assert ctx.phone_e164 == "+919000010006"


def test_provisioning_is_idempotent(db: sqlite3.Connection) -> None:
    provision_identities(db)
    provision_identities(db)
    assert db.execute("select count(*) from channel_identities").fetchone()[0] == 15


def test_unknown_phone_rejected(db: sqlite3.Connection) -> None:
    provision_identities(db)
    with pytest.raises(AuthError) as err:
        resolve_driver(db, "+919999999999")
    assert err.value.code == "UNKNOWN_PHONE"


def test_suspended_driver_rejected(db: sqlite3.Connection) -> None:
    provision_identities(db)
    db.execute("update drivers set driver_status='SUSPENDED' where driver_id='DRV006'")
    db.commit()
    with pytest.raises(AuthError) as err:
        resolve_driver(db, "+919000010006")
    assert err.value.code == "DRIVER_NOT_ALLOWED"


# --- sessions ----------------------------------------------------------------------------------
def test_session_is_server_generated_and_bound_to_one_driver(db: sqlite3.Connection, clock: FakeClock) -> None:
    s1 = create_session(db, "DRV006", clock)
    s2 = create_session(db, "DRV006", clock)
    assert s1.token != s2.token and len(s1.token) >= 32
    assert authenticate_session(db, s1.token, clock).driver_id == "DRV006"


def test_raw_token_is_not_stored(db: sqlite3.Connection, clock: FakeClock) -> None:
    s = create_session(db, "DRV006", clock)
    stored = [r[0] for r in db.execute("select session_id from driver_sessions")]
    assert s.token not in stored


def test_client_supplied_session_id_is_never_authorization(db: sqlite3.Connection, clock: FakeClock) -> None:
    with pytest.raises(AuthError) as err:
        authenticate_session(db, "DRV006", clock)
    assert err.value.code == "SESSION_INVALID"
    s = create_session(db, "DRV006", clock)
    stored_hash = db.execute("select session_id from driver_sessions").fetchone()[0]
    with pytest.raises(AuthError):  # the stored identifier itself is not a credential
        authenticate_session(db, stored_hash, clock)
    assert s.token != stored_hash


def test_expired_session_rejected(db: sqlite3.Connection, clock: FakeClock) -> None:
    s = create_session(db, "DRV006", clock, ttl=timedelta(minutes=30))
    clock.advance(timedelta(minutes=30))
    with pytest.raises(AuthError) as err:
        authenticate_session(db, s.token, clock)
    assert err.value.code == "SESSION_EXPIRED"


def test_session_for_unknown_or_inactive_driver_cannot_be_created(db: sqlite3.Connection, clock: FakeClock) -> None:
    with pytest.raises(AuthError):
        create_session(db, "DRV999", clock)


# --- cross-driver access -----------------------------------------------------------------------
def test_driver_may_access_own_shipments_only(db: sqlite3.Connection, clock: FakeClock) -> None:
    ctx = authenticate_session(db, create_session(db, "DRV006", clock).token, clock)
    authorize_shipment(db, ctx, "SHP1006")
    authorize_shipment(db, ctx, "SHP1021")
    with pytest.raises(AuthError) as err:
        authorize_shipment(db, ctx, "SHP1012")  # belongs to DRV012
    assert err.value.code == "FOREIGN_SHIPMENT"
    with pytest.raises(AuthError) as err2:
        authorize_shipment(db, ctx, "SHP9999")  # nonexistent looks identical
    assert err2.value.code == "FOREIGN_SHIPMENT"


# --- rate limits -------------------------------------------------------------------------------
def test_message_burst_blocked_after_per_minute_limit(redis_client: fakeredis.FakeRedis, clock: FakeClock) -> None:
    limits = RateLimits(per_minute=20, per_hour=100)
    results = [check_rate_limit(redis_client, "DRV006", clock, limits) for _ in range(21)]
    assert all(r.allowed for r in results[:20])
    assert not results[20].allowed
    assert results[20].reason_code == "RATE_LIMIT_MINUTE"
    assert 0 < results[20].retry_after_seconds <= 60


def test_per_minute_window_resets(redis_client: fakeredis.FakeRedis, clock: FakeClock) -> None:
    limits = RateLimits(per_minute=2, per_hour=100)
    for _ in range(2):
        check_rate_limit(redis_client, "D", clock, limits)
    assert not check_rate_limit(redis_client, "D", clock, limits).allowed
    clock.advance(timedelta(minutes=1))
    assert check_rate_limit(redis_client, "D", clock, limits).allowed


def test_hourly_abuse_limit(redis_client: fakeredis.FakeRedis, clock: FakeClock) -> None:
    limits = RateLimits(per_minute=20, per_hour=100)
    for _ in range(5):
        for _ in range(20):
            assert check_rate_limit(redis_client, "D", clock, limits).allowed
        clock.advance(timedelta(minutes=1))
    blocked = check_rate_limit(redis_client, "D", clock, limits)
    assert not blocked.allowed and blocked.reason_code == "RATE_LIMIT_HOUR"


def test_limits_are_per_driver_and_per_channel(redis_client: fakeredis.FakeRedis, clock: FakeClock) -> None:
    limits = RateLimits(per_minute=1, per_hour=10)
    assert check_rate_limit(redis_client, "A", clock, limits).allowed
    assert not check_rate_limit(redis_client, "A", clock, limits).allowed
    assert check_rate_limit(redis_client, "B", clock, limits).allowed
    assert check_rate_limit(redis_client, "A", clock, limits, channel="sms").allowed


def test_denied_messages_do_not_consume_hourly_budget(redis_client: fakeredis.FakeRedis, clock: FakeClock) -> None:
    limits = RateLimits(per_minute=1, per_hour=3)
    check_rate_limit(redis_client, "D", clock, limits)
    for _ in range(10):  # all denied within the same minute
        check_rate_limit(redis_client, "D", clock, limits)
    clock.advance(timedelta(minutes=1))
    assert check_rate_limit(redis_client, "D", clock, limits).allowed  # 2nd allowed message of 3
