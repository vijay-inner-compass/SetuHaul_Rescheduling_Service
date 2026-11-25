"""Telegram driver channel: binding by shared contact, eligibility, and the client that sends replies."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

# Enum for the bind outcome; Protocol for the injectable client; logging for failures (never the token).
import logging
import sqlite3
from datetime import date
from enum import Enum
from typing import Any, Protocol

import httpx

from setuhaul.auth import audit
from setuhaul.auth.onboarding import driver_eligible
from setuhaul.auth.phone import normalise_e164
from setuhaul.auth.users import User
from setuhaul.clock import Clock
from setuhaul.db import immediate
from setuhaul.identity import AuthError, TrustedDriverContext, _context_for

_LOG = logging.getLogger(__name__)

# What a driver is told. The refusal is deliberately the same for every cause, so it never reveals who is registered.
NEUTRAL_REFUSAL = "Sorry, I can't link or help this account. Please contact your warehouse coordinator."
SHARE_PROMPT = "To use SetuHaul, please share your phone number with the button below so I can link this chat to your driver profile."
BOUND_REPLY = "Thanks, this chat is now linked to your driver profile. How can I help?"
TOO_LONG_REPLY = "That message is too long. Please send a shorter one."


class BindResult(Enum):
    # The contact was accepted and the chat is now bound.
    BOUND = "BOUND"
    # Refused; the reason is in the audit row only.
    REFUSED = "REFUSED"


class TelegramSendError(Exception):
    """A reply could not be sent. The text never contains the bot token."""


class TelegramClient(Protocol):
    # Send one message; with request_contact_button the chat shows a "Share my phone number" keyboard button.
    def send(self, chat_id: int, text: str, request_contact_button: bool = False) -> None: ...


class FakeTelegramClient:
    """Records messages instead of sending them (tests and the demo)."""

    def __init__(self) -> None:
        # (chat_id, text, request_contact_button) in send order.
        self.sent: list[tuple[int, str, bool]] = []

    def send(self, chat_id: int, text: str, request_contact_button: bool = False) -> None:
        self.sent.append((chat_id, text, request_contact_button))


class RealTelegramClient:
    """Posts to the Telegram Bot API. The token lives only in this object and never reaches a log, audit row or error text."""

    def __init__(self, token: str, http: httpx.Client) -> None:
        # httpx logs every request URL at INFO, and the URL carries the token: keep those loggers quiet.
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.WARNING)
        self._token = token
        self._http = http

    def __repr__(self) -> str:
        # Never show the token.
        return "RealTelegramClient()"

    def send(self, chat_id: int, text: str, request_contact_button: bool = False) -> None:
        body: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if request_contact_button:
            # A one-time reply keyboard with a single contact-request button.
            body["reply_markup"] = {"keyboard": [[{"text": "Share my phone number", "request_contact": True}]],
                                    "resize_keyboard": True, "one_time_keyboard": True}
        else:
            # Remove any earlier keyboard.
            body["reply_markup"] = {"remove_keyboard": True}
        try:
            resp = self._http.post(f"https://api.telegram.org/bot{self._token}/sendMessage", json=body)
        except httpx.HTTPError as exc:
            # httpx messages can quote the URL, which contains the token: report only the error class.
            raise TelegramSendError(f"sendMessage failed: {type(exc).__name__}") from None
        if resp.status_code >= 300:
            # Telegram's description is not copied: it could echo request data.
            raise TelegramSendError(f"sendMessage failed: HTTP {resp.status_code}")


def _audit(conn: sqlite3.Connection, clock: Clock, action: str, outcome: str, driver_id: str | None, detail: dict[str, object]) -> None:
    # System-initiated: no web actor. The detail carries the reason code and the Telegram id, never a phone number.
    audit.record(conn, actor_user_id=None, actor_email=None, action=action, target_type="driver", target_id=driver_id,
                 outcome=outcome, ip=None, detail=detail, clock=clock)


def bind_from_contact(conn: sqlite3.Connection, *, telegram_user_id: int, chat_id: int, contact_user_id: int | None,
                      contact_phone: str, clock: Clock) -> BindResult:
    """Bind the sender to the driver whose number they shared. The reason for any refusal goes to the audit row only."""
    today = clock.now().date()
    uid = str(telegram_user_id)

    def refuse(reason: str, driver_id: str | None = None) -> BindResult:
        _audit(conn, clock, "TELEGRAM_BIND_REFUSED", "REFUSED", driver_id, {"reason": reason, "telegram_user_id": uid})
        return BindResult.REFUSED

    # One write transaction so two simultaneous contacts cannot both bind the same driver or account.
    with immediate(conn):
        # The contact card must be the sender's own: a forwarded card carries another user id or none.
        if contact_user_id is None or contact_user_id != telegram_user_id:
            return refuse("CONTACT_NOT_OWN")
        phone = normalise_e164(contact_phone)
        if phone is None:
            return refuse("INVALID_PHONE")
        # Compare normalised forms: supplied rows are stored hyphenated.
        matches = [r["driver_id"] for r in conn.execute("select driver_id, phone from drivers order by driver_id")
                   if r["phone"] and normalise_e164(r["phone"]) == phone]
        if len(matches) != 1:
            return refuse("NO_MATCH" if not matches else "AMBIGUOUS_PHONE")
        driver_id = matches[0]
        if not driver_eligible(conn, driver_id, today):
            return refuse("NOT_ELIGIBLE", driver_id)
        # A revoke is sticky: while the driver's binding row is revoked nobody (this account or another) may bind them,
        # until an audited allow-relink clears the row.
        existing = conn.execute("select telegram_user_id, revoked_at from telegram_identities where driver_id=?", (driver_id,)).fetchone()
        if existing is not None and existing["revoked_at"] is not None:
            return refuse("REVOKED", driver_id)
        # An account revoked for another driver stays cut off; binding it elsewhere would have to erase that revoke.
        if conn.execute("select 1 from telegram_identities where telegram_user_id=? and revoked_at is not null", (uid,)).fetchone():
            return refuse("SENDER_REVOKED", driver_id)
        # This Telegram account may hold only one active binding.
        if conn.execute("select 1 from telegram_identities where telegram_user_id=? and revoked_at is null", (uid,)).fetchone():
            return refuse("SENDER_ALREADY_BOUND", driver_id)
        # The driver may have only one active binding too.
        if existing is not None:
            return refuse("DRIVER_ALREADY_BOUND", driver_id)
        # Neither the driver nor the account has any row now, so this is always a fresh insert.
        conn.execute("insert into telegram_identities(telegram_user_id, chat_id, driver_id, bound_at) values (?,?,?,?)",
                     (uid, str(chat_id), driver_id, clock.now().isoformat()))
        # Never report success unless the binding really exists now; raising rolls the transaction back.
        final = conn.execute("select 1 from telegram_identities where telegram_user_id=? and driver_id=? and revoked_at is null",
                             (uid, driver_id)).fetchone()
        if final is None:
            raise AuthError("BIND_FAILED")
        _audit(conn, clock, "TELEGRAM_BIND_OK", "OK", driver_id, {"telegram_user_id": uid})
    return BindResult.BOUND


def has_binding(conn: sqlite3.Connection, telegram_user_id: int) -> bool:
    # True for a revoked binding as well: the sender is known, just not allowed.
    return conn.execute("select 1 from telegram_identities where telegram_user_id=?", (str(telegram_user_id),)).fetchone() is not None


def resolve_driver(conn: sqlite3.Connection, telegram_user_id: int, today: date) -> str | None:
    """The driver this Telegram account may act as right now, or None (unbound, revoked, or no longer eligible)."""
    row = conn.execute("select driver_id from telegram_identities where telegram_user_id=? and revoked_at is null",
                       (str(telegram_user_id),)).fetchone()
    if row is None or not driver_eligible(conn, row["driver_id"], today):
        return None
    return str(row["driver_id"])


def bound_chat(conn: sqlite3.Connection, driver_id: str) -> int | None:
    # The chat to notify for a driver, or None if they have no active binding.
    row = conn.execute("select chat_id from telegram_identities where driver_id=? and revoked_at is null", (driver_id,)).fetchone()
    return int(row["chat_id"]) if row else None


def trusted_context(conn: sqlite3.Connection, driver_id: str) -> TrustedDriverContext:
    # The conversation's trusted driver context, built from the database for a driver the binding resolved.
    return _context_for(conn, driver_id)


# Longest caller-supplied driver id kept in a probe audit row.
_MAX_TARGET = 64


def may_manage_binding(conn: sqlite3.Connection, actor: User, driver_id: str) -> bool:
    """True if the actor may revoke or allow re-linking this driver's Telegram binding.

    An active admin: any existing driver. An active warehouse authority: only drivers they onboarded themselves
    (allocation to their project does not count). Supplied drivers have no creator, so only an admin.
    """
    if actor.status != "ACTIVE":
        return False
    if actor.role == "ADMIN":
        return conn.execute("select 1 from drivers where driver_id=?", (driver_id,)).fetchone() is not None
    if actor.role != "WAREHOUSE_AUTHORITY":
        return False
    return conn.execute("select 1 from driver_profiles where driver_id=? and created_by_user_id=?",
                        (driver_id, actor.user_id)).fetchone() is not None


def _check_manage(conn: sqlite3.Connection, actor: User, driver_id: str, action: str, clock: Clock) -> None:
    # A wrong role or a disabled account is an audited FORBIDDEN, committed before the error leaves.
    if actor.status != "ACTIVE" or actor.role not in ("ADMIN", "WAREHOUSE_AUTHORITY"):
        audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action=action, target_type="driver",
                     target_id=driver_id[:_MAX_TARGET], outcome="FORBIDDEN", ip=None, detail={"role": actor.role}, clock=clock)
        conn.commit()
        raise AuthError("FORBIDDEN")
    if may_manage_binding(conn, actor, driver_id):
        return
    # Out of scope looks exactly like a driver that does not exist; a probe of a real driver is audited.
    if conn.execute("select 1 from drivers where driver_id=?", (driver_id,)).fetchone() is not None:
        audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action=action, target_type="driver",
                     target_id=driver_id, outcome="NOT_FOUND", ip=None, detail={"role": actor.role}, clock=clock)
        conn.commit()
    raise AuthError("DRIVER_NOT_FOUND")


def revoke(conn: sqlite3.Connection, *, actor: User, driver_id: str, clock: Clock) -> None:
    """Cut a driver's Telegram binding (admin, or the warehouse authority who onboarded the driver). The cut is sticky."""
    _check_manage(conn, actor, driver_id, "TELEGRAM_REVOKED", clock)
    with immediate(conn):
        cur = conn.execute("update telegram_identities set revoked_at=? where driver_id=? and revoked_at is null",
                           (clock.now().isoformat(), driver_id))
        if cur.rowcount == 0:
            raise AuthError("NOT_BOUND")
        audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action="TELEGRAM_REVOKED", target_type="driver",
                     target_id=driver_id, outcome="OK", ip=None, detail=None, clock=clock)


def allow_relink(conn: sqlite3.Connection, *, actor: User, driver_id: str, clock: Clock) -> None:
    """Clear a driver's revoked binding so they may share their contact again (same scope rule as revoke)."""
    _check_manage(conn, actor, driver_id, "TELEGRAM_RELINK_ALLOWED", clock)
    with immediate(conn):
        row = conn.execute("select telegram_user_id, revoked_at from telegram_identities where driver_id=?", (driver_id,)).fetchone()
        if row is None:
            raise AuthError("NOT_BOUND")
        # Only a revoked binding is cleared; an active one stays as it is.
        if row["revoked_at"] is None:
            raise AuthError("NOT_REVOKED")
        conn.execute("delete from telegram_identities where driver_id=? and revoked_at is not null", (driver_id,))
        # The cleared account id is kept in the trail, so the history survives the delete.
        audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action="TELEGRAM_RELINK_ALLOWED", target_type="driver",
                     target_id=driver_id, outcome="OK", ip=None, detail={"telegram_user_id": row["telegram_user_id"]}, clock=clock)
