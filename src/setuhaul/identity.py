"""Driver identity from verified channels and server-created sessions.

The model never supplies ``driver_id``: it is derived from a verified webhook signature plus the
provisioned phone map, or from a server-issued session token.
"""

# Lazy annotations.
from __future__ import annotations

# hashlib: SHA-256 for signatures and for hashing session tokens before storage.
import hashlib

# hmac: keyed signature creation and constant-time comparison.
import hmac

# re: strips non-digits from phone numbers.
import re

# secrets: cryptographically strong random session tokens.
import secrets

# sqlite3 for the Connection type.
import sqlite3

# timedelta for session lifetimes.
from datetime import timedelta

# Frozen validated models for the trusted contexts.
from pydantic import BaseModel, ConfigDict

# The injectable clock, so session expiry is testable.
from setuhaul.clock import Clock

# Parses stored timestamps.
from setuhaul.db import parse_operational_timestamp

# Only drivers in these states may use the service; suspended or deleted drivers are refused.
ALLOWED_DRIVER_STATUSES = ("ACTIVE", "OFF_DUTY")
# A server-issued session lasts 8 hours (one shift).
DEFAULT_SESSION_TTL = timedelta(hours=8)


class AuthError(Exception):
    """Authentication or authorisation failure with a stable reason code."""

    def __init__(self, code: str, message: str = "") -> None:
        # The text is "CODE: message" (or just "CODE"), so logs are readable.
        super().__init__(f"{code}: {message}" if message else code)
        # The code is kept separately so callers can branch on it without parsing text.
        self.code = code


# What the rest of the system may trust about the caller. It is built only from the database after verification,
# never from anything the driver or the model typed.
class TrustedDriverContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    driver_id: str
    carrier_id: str
    phone_e164: str


# A session as returned to the client: the plain token (shown once) and when it expires.
class SessionContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    token: str
    driver_id: str
    expires_at: str


def verify_channel_request(body: bytes, signature: str, secret: bytes) -> bool:
    """HMAC-SHA256 hex signature check over the raw body, before any parsing."""
    # No signature at all is rejected with its own message.
    if not signature:
        raise AuthError("INVALID_SIGNATURE", "missing signature")
    # What the signature should be if the sender knows the shared secret.
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    # compare_digest takes the same time however many characters match, so an attacker cannot learn the signature by timing.
    if not hmac.compare_digest(expected, signature.strip().lower()):
        raise AuthError("INVALID_SIGNATURE")
    return True


def normalize_phone(raw: str) -> str:
    """Normalise to E.164. Bare 10-digit numbers are treated as Indian (+91)."""
    # None or blank becomes an empty string, which fails the length checks below.
    text = (raw or "").strip()
    # A leading "+" means the number already carries its country code.
    plus = text.startswith("+")
    # Keep digits only: spaces, dashes and brackets are formatting.
    digits = re.sub(r"\D", "", text)
    # "00" is the international call prefix, equivalent to "+".
    if digits.startswith("00"):
        digits, plus = digits[2:], True
    # Without an international prefix, work out the country code ourselves.
    if not plus:
        # Ten digits is an Indian mobile number without its country code.
        if len(digits) == 10:
            digits = "91" + digits
        # Eleven digits starting with 0 is the Indian trunk-prefix format (0 + 10 digits).
        elif len(digits) == 11 and digits.startswith("0"):
            digits = "91" + digits[1:]
        # Twelve digits starting with 91 already includes the country code; anything else is not recognised.
        elif not (len(digits) == 12 and digits.startswith("91")):
            raise AuthError("INVALID_PHONE", "unrecognised phone format")
    # E.164 numbers are 10 to 15 digits; anything outside that is not a real phone number.
    if not 10 <= len(digits) <= 15:
        raise AuthError("INVALID_PHONE", "unrecognised phone format")
    return "+" + digits


def provision_identities(conn: sqlite3.Connection) -> int:
    """Populate the phone -> driver map from drivers.phone (idempotent). Returns map size."""
    # Every driver with their stored phone number.
    rows = conn.execute("select driver_id, phone from drivers").fetchall()
    for driver_id, phone in rows:
        # "on conflict do nothing" makes this safe to run repeatedly: existing mappings are left alone.
        conn.execute(
            "insert into channel_identities (phone_e164, driver_id, provisioned_at) "
            "values (?, ?, datetime('now')) on conflict do nothing",
            (normalize_phone(phone), driver_id),
        )
    # Make the new mappings permanent.
    conn.commit()
    # Return how many mappings exist now.
    return int(conn.execute("select count(*) from channel_identities").fetchone()[0])


def _context_for(conn: sqlite3.Connection, driver_id: str) -> TrustedDriverContext:
    # Load the driver record that the identity maps to.
    row = conn.execute(
        "select d.driver_id, d.carrier_id, d.phone, d.driver_status from drivers d where d.driver_id=?",
        (driver_id,),
    ).fetchone()
    # A mapping that points at a missing driver is refused.
    if row is None:
        raise AuthError("UNKNOWN_DRIVER")
    # Suspended or inactive drivers may not use the service even with a valid phone.
    if row["driver_status"] not in ALLOWED_DRIVER_STATUSES:
        raise AuthError("DRIVER_NOT_ALLOWED", row["driver_status"])
    # The trusted context carries everything later code needs, so it never has to ask the model for an identity.
    return TrustedDriverContext(
        driver_id=row["driver_id"], carrier_id=row["carrier_id"], phone_e164=normalize_phone(row["phone"])
    )


def resolve_driver(conn: sqlite3.Connection, raw_phone: str) -> TrustedDriverContext:
    # Normalise first so "98765 43210", "+91 98765 43210" and "09876543210" all find the same driver.
    phone = normalize_phone(raw_phone)
    # Look the number up in the provisioned map.
    row = conn.execute("select driver_id from channel_identities where phone_e164=?", (phone,)).fetchone()
    # An unprovisioned number is not a known driver: refuse, do not create one.
    if row is None:
        raise AuthError("UNKNOWN_PHONE")
    return _context_for(conn, row["driver_id"])


def _hash(token: str) -> str:
    # Only the hash of a session token is stored, so a copy of the database cannot be used to impersonate anyone.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(
    conn: sqlite3.Connection, driver_id: str, clock: Clock, ttl: timedelta = DEFAULT_SESSION_TTL
) -> SessionContext:
    _context_for(conn, driver_id)  # raises for unknown or disallowed drivers
    # 32 random bytes, URL-safe: infeasible to guess.
    token = secrets.token_urlsafe(32)
    # Created and expiry times come from the injected clock.
    now = clock.now()
    expires = now + ttl
    # Store the hash (as the session id), never the token itself.
    conn.execute(
        "insert into driver_sessions (session_id, driver_id, created_at, expires_at) values (?,?,?,?)",
        (_hash(token), driver_id, now.isoformat(), expires.isoformat()),
    )
    conn.commit()
    # The plain token is returned to the caller once; it cannot be recovered later.
    return SessionContext(token=token, driver_id=driver_id, expires_at=expires.isoformat())


def authenticate_session(conn: sqlite3.Connection, token: str, clock: Clock) -> TrustedDriverContext:
    # Look up by the hash of the presented token.
    row = conn.execute(
        "select driver_id, expires_at, revoked_at from driver_sessions where session_id=?", (_hash(token),)
    ).fetchone()
    # Unknown token and revoked token get the same answer, so a caller cannot tell which tokens ever existed.
    if row is None or row["revoked_at"] is not None:
        raise AuthError("SESSION_INVALID")
    # Past its expiry time: refuse.
    if parse_operational_timestamp(row["expires_at"]) <= clock.now():
        raise AuthError("SESSION_EXPIRED")
    # Re-check the driver's status now: a driver suspended after the session was issued loses access.
    return _context_for(conn, row["driver_id"])


def authorize_shipment(conn: sqlite3.Connection, ctx: TrustedDriverContext, shipment_id: str) -> None:
    """Missing and foreign shipments are indistinguishable to the caller."""
    # The shipment must exist AND belong to this driver; one query checks both.
    row = conn.execute(
        "select 1 from shipments where shipment_id=? and driver_id=?", (shipment_id, ctx.driver_id)
    ).fetchone()
    # One error for both "no such shipment" and "someone else's shipment", so ids cannot be probed.
    if row is None:
        raise AuthError("FOREIGN_SHIPMENT")
