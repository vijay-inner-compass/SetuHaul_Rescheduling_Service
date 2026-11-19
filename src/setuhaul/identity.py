"""Driver identity from verified channels and server-created sessions.

The model never supplies ``driver_id``: it is derived from a verified webhook signature plus the
provisioned phone map, or from a server-issued session token.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from datetime import timedelta

from pydantic import BaseModel, ConfigDict

from setuhaul.clock import Clock
from setuhaul.db import parse_operational_timestamp

ALLOWED_DRIVER_STATUSES = ("ACTIVE", "OFF_DUTY")
DEFAULT_SESSION_TTL = timedelta(hours=8)


class AuthError(Exception):
    """Authentication or authorisation failure with a stable reason code."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


class TrustedDriverContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    driver_id: str
    carrier_id: str
    phone_e164: str


class SessionContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    token: str
    driver_id: str
    expires_at: str


def verify_channel_request(body: bytes, signature: str, secret: bytes) -> bool:
    """HMAC-SHA256 hex signature check over the raw body, before any parsing."""
    if not signature:
        raise AuthError("INVALID_SIGNATURE", "missing signature")
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.strip().lower()):
        raise AuthError("INVALID_SIGNATURE")
    return True


def normalize_phone(raw: str) -> str:
    """Normalise to E.164. Bare 10-digit numbers are treated as Indian (+91)."""
    text = (raw or "").strip()
    plus = text.startswith("+")
    digits = re.sub(r"\D", "", text)
    if digits.startswith("00"):
        digits, plus = digits[2:], True
    if not plus:
        if len(digits) == 10:
            digits = "91" + digits
        elif len(digits) == 11 and digits.startswith("0"):
            digits = "91" + digits[1:]
        elif not (len(digits) == 12 and digits.startswith("91")):
            raise AuthError("INVALID_PHONE", "unrecognised phone format")
    if not 10 <= len(digits) <= 15:
        raise AuthError("INVALID_PHONE", "unrecognised phone format")
    return "+" + digits


def provision_identities(conn: sqlite3.Connection) -> int:
    """Populate the phone -> driver map from drivers.phone (idempotent). Returns map size."""
    rows = conn.execute("select driver_id, phone from drivers").fetchall()
    for driver_id, phone in rows:
        conn.execute(
            "insert into channel_identities (phone_e164, driver_id, provisioned_at) "
            "values (?, ?, datetime('now')) on conflict do nothing",
            (normalize_phone(phone), driver_id),
        )
    conn.commit()
    return int(conn.execute("select count(*) from channel_identities").fetchone()[0])


def _context_for(conn: sqlite3.Connection, driver_id: str) -> TrustedDriverContext:
    row = conn.execute(
        "select d.driver_id, d.carrier_id, d.phone, d.driver_status from drivers d where d.driver_id=?",
        (driver_id,),
    ).fetchone()
    if row is None:
        raise AuthError("UNKNOWN_DRIVER")
    if row["driver_status"] not in ALLOWED_DRIVER_STATUSES:
        raise AuthError("DRIVER_NOT_ALLOWED", row["driver_status"])
    return TrustedDriverContext(
        driver_id=row["driver_id"], carrier_id=row["carrier_id"], phone_e164=normalize_phone(row["phone"])
    )


def resolve_driver(conn: sqlite3.Connection, raw_phone: str) -> TrustedDriverContext:
    phone = normalize_phone(raw_phone)
    row = conn.execute("select driver_id from channel_identities where phone_e164=?", (phone,)).fetchone()
    if row is None:
        raise AuthError("UNKNOWN_PHONE")
    return _context_for(conn, row["driver_id"])


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(
    conn: sqlite3.Connection, driver_id: str, clock: Clock, ttl: timedelta = DEFAULT_SESSION_TTL
) -> SessionContext:
    _context_for(conn, driver_id)  # raises for unknown or disallowed drivers
    token = secrets.token_urlsafe(32)
    now = clock.now()
    expires = now + ttl
    conn.execute(
        "insert into driver_sessions (session_id, driver_id, created_at, expires_at) values (?,?,?,?)",
        (_hash(token), driver_id, now.isoformat(), expires.isoformat()),
    )
    conn.commit()
    return SessionContext(token=token, driver_id=driver_id, expires_at=expires.isoformat())


def authenticate_session(conn: sqlite3.Connection, token: str, clock: Clock) -> TrustedDriverContext:
    row = conn.execute(
        "select driver_id, expires_at, revoked_at from driver_sessions where session_id=?", (_hash(token),)
    ).fetchone()
    if row is None or row["revoked_at"] is not None:
        raise AuthError("SESSION_INVALID")
    if parse_operational_timestamp(row["expires_at"]) <= clock.now():
        raise AuthError("SESSION_EXPIRED")
    return _context_for(conn, row["driver_id"])


def authorize_shipment(conn: sqlite3.Connection, ctx: TrustedDriverContext, shipment_id: str) -> None:
    """Missing and foreign shipments are indistinguishable to the caller."""
    row = conn.execute(
        "select 1 from shipments where shipment_id=? and driver_id=?", (shipment_id, ctx.driver_id)
    ).fetchone()
    if row is None:
        raise AuthError("FOREIGN_SHIPMENT")
