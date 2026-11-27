"""HTTP surface: driver webhook, driver status, coordinator queue, /health and /ready.

Order of work on a driver message, cheapest and safest first, always before the model is invoked:
signature (raw bytes, before any parsing) -> sender lookup -> duplicate delivery -> rate limit ->
claim the message id -> conversation. Errors are structured ({status, reason_code, request_id});
no stack trace, prompt, SQL or file path ever leaves the process.
"""

# Lazy annotations.
from __future__ import annotations

# hashlib fingerprints message text for idempotency.
import hashlib

# hmac.compare_digest compares coordinator tokens in constant time.
import hmac

# json encodes and decodes request and stored reply bodies.
import json
import logging

# os reads environment configuration.
import os

# sqlite3 for connections and database errors.
import sqlite3
import threading

# uuid makes request ids.
import uuid

# Typing helpers.
from collections.abc import AsyncIterator, Callable, Mapping

# Builds the application's start-up and shut-down hook.
from contextlib import asynccontextmanager

# dataclass for the dependency bundle.
from dataclasses import dataclass, field

# Re-exported below.
from datetime import timedelta

# Paths for the database and fallback file.
from pathlib import Path

# Typing helper.
from typing import Any

# FastAPI is the web framework.
from fastapi import Depends, FastAPI, Request

# The error raised when a request body fails validation.
from fastapi.exceptions import RequestValidationError

# JSON responses.
from fastapi.responses import JSONResponse

# Request body models.
from pydantic import BaseModel, ConfigDict, Field, ValidationError

# The Redis client type.
from redis import Redis

# Runs blocking database code in a worker thread so the event loop is not blocked.
from starlette.concurrency import run_in_threadpool

# The Dock Access Pass check that runs before the conversation.
from setuhaul import assign, chat_loads, pass_check

# Web sessions, the role guard and the facility scope of cases.
from setuhaul.auth import refusals, same_origin, sessions
from setuhaul.auth.account_mail import account_sender_from_env
from setuhaul.auth.authz import AuthzError, require_role
from setuhaul.auth.google import GoogleVerifier
from setuhaul.auth.onboarding import driver_eligible

# The Telegram reply sender type.
from setuhaul.auth.telegram import TelegramClient
from setuhaul.auth.users import User
from setuhaul.auth.warehouse_routes import case_ids_in_scope
from setuhaul.booking import make_hold_checker

# Real or simulated clock.
from setuhaul.clock import Clock, SystemClock

# Settings.
from setuhaul.config import load_settings, sample_database_problem

# The conversation workflow.
from setuhaul.conversation import (
    ConversationServices,
    InboundMessage,
    TrustedConversationContext,
    handle_message,
)

# Database helpers.
from setuhaul.db import IST, immediate, open_connection, parse_operational_timestamp

# Blanking pass codes before anything is kept.
from setuhaul.dock_pass import HIDDEN, blank

# Escalation queue operations.
from setuhaul.escalation import (
    JsonlEscalationSink,
    SqlEscalationSink,
    TransitionResult,
    claim_case,
    get_case,
    get_driver_case_status,
    list_open_cases,
    reopen_case,
    resolve_case,
)

# GPS ping storage and the driver's active shipment.
from setuhaul.gps import UnknownShipment, active_shipment_for_driver, store_ping

# Phone-to-driver lookup and webhook signature check.
from setuhaul.identity import (
    AuthError,
    TrustedDriverContext,
    resolve_driver,
    verify_channel_request,
)

# The background maintenance runner.
from setuhaul.jobs.expire_holds import MaintenanceRunner

# The background sender of queued driver notices.
from setuhaul.jobs.partner_mail import PartnerMailRunner
from setuhaul.jobs.send_driver_notifications import DriverNoticeRunner

# The driver's language, for the fixed pass replies.
from setuhaul.language import LANGS, detect_language, say

# The shape of anything that can send an email.
from setuhaul.mail_transport import MailReader, MailSender, build_mail
from setuhaul.mailcfg import load_mail_settings

# Conversation memory.
from setuhaul.memory import ConversationMemoryStore

# Model types.
from setuhaul.model_gateway import (
    ModelGateway,
    ModelPolicy,
    UnconfiguredGateway,
)

# Records model calls for the admin Model tab.
from setuhaul.model_usage import TurnRecorder

# Span exporter type.
from setuhaul.observability import InMemoryExporter, configure_logging

# Per-driver rate limiting.
# A new truck position: confirmation, hold release and earlier-slot offers.
from setuhaul.position import handle_driver_position, handle_new_position

# Per-driver rate limiting.
from setuhaul.rate_limit import RateLimits, check_rate_limit
from setuhaul.runtime_settings import MIN_TELEGRAM_SECRET_LEN, SettingsSource

# The validated ping shape.
from setuhaul.schemas import GpsPing
from setuhaul.webhook_startup import register_at_start

# A message stuck in PROCESSING for this long is presumed abandoned and may be reclaimed.
STALE_PROCESSING_SECONDS = 60
# A signed status request older than this (5 minutes) is rejected, so a captured request cannot be replayed later.
SIGNATURE_WINDOW_SECONDS = 300
# A GPS ping may be stamped at most this far (5 minutes) ahead of the server clock; allows small device clock drift only.
GPS_MAX_FUTURE_SECONDS = 300


@dataclass(frozen=True)
class PasswordMail:
    # None: password links cannot be sent; `missing` then names the settings to fill in (never their values).
    sender: MailSender | None
    missing: tuple[str, ...]


@dataclass
# Everything the app needs, passed in explicitly so tests and the demo can substitute parts.
class AppDeps:
    # The working database.
    db_path: Path
    # Cache and rate-limit store.
    redis: Redis
    # Shared secret for the channel's HMAC signature.
    webhook_secret: bytes
    coordinator_tokens: dict[str, str]  # bearer token -> coordinator name
    # Where hand-offs go if the database is down.
    fallback_path: Path
    # Model limits.
    policy: ModelPolicy = field(default_factory=ModelPolicy)
    # Builds the model gateway for each message.
    gateway_factory: Callable[[], ModelGateway] = UnconfiguredGateway
    # The real service's saved settings; None (tests, demo) uses policy and gateway_factory as given.
    settings: SettingsSource | None = None
    # The clock.
    clock: Clock = field(default_factory=SystemClock)
    # Override for the rate limits.
    rate_limits: RateLimits | None = None
    # Channel name, part of the rate-limit key.
    channel: str = "whatsapp"
    # Start the background hold-expiry sweep.
    run_maintenance: bool = False
    # Seconds between sweeps.
    maintenance_interval_s: float | None = None
    # Optional span collector.
    exporter: InMemoryExporter | None = None
    demo: Any = None  # a demo_ui.DemoState turns on the localhost-only demo screen
    # Google sign-in verifier; None leaves the sign-in routes answering 503.
    google: GoogleVerifier | None = None
    # Key for the signed OAuth state cookie.
    session_secret: bytes = b""
    # The site's public origin (SETUHAUL_PUBLIC_URL); when set it fixes the OAuth redirect URI and the Secure cookie flag.
    public_url: str = ""
    # The break-glass admin password route exists only when this is on (SETUHAUL_LOCAL_ADMIN=1).
    local_admin_enabled: bool = False
    # Sends password links; None leaves email and password sign-in off (SETUHAUL_PASSWORD_LOGIN).
    password_sender: MailSender | None = None
    # Shared secret Telegram echoes in a header on every webhook call; empty leaves the Telegram route off.
    telegram_secret: bytes = b""
    # Sends replies to Telegram chats; None means no replies can be sent.
    telegram: TelegramClient | None = None
    # Send queued driver notices from a background thread (needs a Telegram client; one server process only).
    run_driver_notices: bool = False
    # Seconds between delivery passes.
    notice_interval_s: float = 5.0
    # Tell Telegram this service's webhook address at start-up (needs settings and public_url).
    # Tell Telegram this service's webhook address at start-up (needs settings and public_url).
    auto_register_webhook: bool = False
    # Partner-warehouse email transport (sender, reader); None leaves the partner mail runner off.
    partner_mail: tuple[MailSender, MailReader] | None = None
    # Start the background partner-mail runner (send requests, poll replies, expire overdue requests).
    run_partner_mail: bool = False
    # Seconds between partner-mail passes.
    partner_mail_interval_s: float = 30.0
    # Require Authentication-Results (DMARC/DKIM) on partner replies: true in live mode, false in simulated tests.
    partner_mail_require_auth: bool = False
    # Receiving server name checked in Authentication-Results when partner_mail_require_auth is true.
    partner_mail_auth_serv_id: str = "mx.google.com"

    def password_mail(self) -> PasswordMail:
        """The sender for password links in force now: the screen's saved mailbox over the environment's."""
        # A sender given outright (tests) is used as it is.
        if self.password_sender is not None:
            return PasswordMail(self.password_sender, ())
        if self.settings is None:
            return PasswordMail(None, ("SETUHAUL_PASSWORD_LOGIN",))
        try:
            conn = open_connection(path=self.db_path, readonly=True)
            try:
                env = self.settings.effective(conn).env
            finally:
                conn.close()
        except (sqlite3.Error, FileNotFoundError):
            # The sign-in page must still draw: the environment alone is used.
            env = self.settings.base_env
        sender, missing = account_sender_from_env(env, self.settings.mail)
        if env.get("SETUHAUL_PASSWORD_LOGIN") != "1":
            missing = ("SETUHAUL_PASSWORD_LOGIN",)
        return PasswordMail(sender, missing)

    @property
    def password_login_on(self) -> bool:
        # Links are built from the public URL, so without it there is nothing to send.
        return bool(self.public_url) and self.password_mail().sender is not None


# The inbound webhook body. Unknown fields are rejected (extra='forbid').
def model_in_force(deps: AppDeps, conn: sqlite3.Connection) -> tuple[ModelGateway, ModelPolicy]:
    """The gateway and limits for the next call."""
    if deps.settings is None:
        return deps.gateway_factory(), deps.policy
    return deps.settings.model(conn)


def env_in_force(deps: AppDeps, conn: sqlite3.Connection) -> Mapping[str, str] | None:
    """The settings in force, or None to read the process environment."""
    return None if deps.settings is None else deps.settings.effective(conn).env


def telegram_in_force(deps: AppDeps) -> tuple[bytes, TelegramClient | None]:
    """The webhook secret and reply client to use now."""
    if deps.settings is None:
        return deps.telegram_secret, deps.telegram
    conn = open_connection(path=deps.db_path, readonly=True)
    try:
        return deps.settings.telegram(conn)
    finally:
        conn.close()


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # Sender phone number (JSON key 'from').
    sender: str = Field(alias="from", min_length=5, max_length=32)
    # The channel's message id, used for duplicate detection.
    message_id: str = Field(min_length=1, max_length=128)
    # The driver's message, 1 to 2000 characters.
    text: str = Field(min_length=1, max_length=2000)


# A telematics ping body: GpsPing's validated fields (range, aware time, extra='forbid') plus the shipment id.
class TelematicsPingIn(GpsPing):
    # The shipment the vehicle is serving.
    shipment_id: str = Field(min_length=1, max_length=64)


# A driver-shared location body: GpsPing's fields plus the sender phone (JSON key 'from').
class DriverLocationIn(GpsPing):
    model_config = ConfigDict(populate_by_name=True)

    # Sender phone number.
    sender: str = Field(alias="from", min_length=5, max_length=32)


# Body of the coordinator 'resolve' call.
class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution_code: str
    note: str = Field(max_length=500)
    # The slot to book, only with the ASSIGNED_SLOT outcome.
    slot_id: str | None = None


# Claims made from a web session are stored as this prefix plus the user's (unique) e-mail.
WEB_PREFIX = "web:"


# A human label for a stored claim identity: a web user's display name, a bearer coordinator's own name.
def claim_label(conn: sqlite3.Connection, claimed_by: str | None) -> str | None:
    if claimed_by is None or not claimed_by.startswith(WEB_PREFIX):
        return claimed_by
    from setuhaul.auth import users as web_users

    found = web_users.find_by_email(conn, claimed_by[len(WEB_PREFIX):])
    return (found.display_name or found.email) if found else claimed_by[len(WEB_PREFIX):]


# A structured error body: status, reason code and request id. No stack trace, SQL or path ever appears.
def _reject(request_id: str, status: int, reason: str, **extra: Any) -> JSONResponse:
    # Rate-limit rejections tell the client when to retry.
    headers = {"Retry-After": str(extra["retry_after_seconds"])} if "retry_after_seconds" in extra else None
    # The rejection response.
    return JSONResponse({"status": "REJECTED", "reason_code": reason, "request_id": request_id, **extra}, status_code=status,
                        headers=headers)


# Build the FastAPI application from its dependencies.
def _record_turn(conn: sqlite3.Connection, memory: ConversationMemoryStore, driver_id: str, thread_id: str, before: int,
                 about: str | None = None) -> None:
    """Tag the turn's messages with their load, so that load's warehouse sees them. Never fails the driver's message."""
    try:
        selected = about or memory.load(driver_id, thread_id).state.selected_shipment_id
        chat_loads.record(conn, thread_id, before, about or chat_loads.load_of_turn(conn, driver_id, selected))
        conn.commit()
    except Exception:
        logging.getLogger(__name__).warning("chat message load not recorded", exc_info=True)


def resolve_eligible_driver(conn: sqlite3.Connection, raw_phone: str, clock: Clock) -> TrustedDriverContext:
    """The phone channel's driver, refused like an inactive one unless eligible today (IST): approved with a valid licence.

    Supplied drivers (no onboarding profile) stay eligible; the rule is the one allocation and the Telegram bot use.
    """
    # Phone lookup and the driver_status check first, unchanged.
    trusted = resolve_driver(conn, raw_phone)
    # Then the onboarding rule; the refusal is the same code an inactive driver gets.
    if not driver_eligible(conn, trusted.driver_id, clock.now().astimezone(IST).date()):
        raise AuthError("DRIVER_NOT_ALLOWED", "NOT_ELIGIBLE")
    return trusted


def create_app(deps: AppDeps) -> FastAPI:
    # Start-up and shut-down hook.
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # No background runner unless enabled.
        runner = None
        # Start the hold-expiry sweep in a background thread.
        # With saved settings a bot may appear at any time, so both runners always start.
        if deps.run_maintenance or deps.settings is not None:
            # The runner opens its own connection (SQLite connections cannot cross threads).
            runner = MaintenanceRunner(None, deps.clock, deps.maintenance_interval_s,
                                       conn_factory=lambda: open_connection(path=deps.db_path),
                                       fallback_path=deps.fallback_path)
            # Start it off the event loop.
            await run_in_threadpool(runner.start)
        # Messages written outside a driver's turn reach their phone only through this sender.
        notices = None
        if deps.settings is not None:
            notices = DriverNoticeRunner(deps.clock, None, lambda: open_connection(path=deps.db_path), deps.notice_interval_s,
                                         client_source=lambda: telegram_in_force(deps)[1])
            notices.start()
        elif deps.run_driver_notices and deps.telegram is not None:
            notices = DriverNoticeRunner(deps.clock, deps.telegram, lambda: open_connection(path=deps.db_path), deps.notice_interval_s)
            notices.start()
        # On its own thread, so a slow or unreachable mail server never delays hold expiry or driver notices.
        partner = None
        if deps.run_partner_mail and deps.partner_mail is not None:
            p_sender, p_reader = deps.partner_mail
            partner = PartnerMailRunner(
                None, deps.clock, p_sender, p_reader,
                interval_seconds=deps.partner_mail_interval_s,
                conn_factory=lambda: open_connection(path=deps.db_path),
                require_auth=deps.partner_mail_require_auth,
                auth_serv_id=deps.partner_mail_auth_serv_id,
            )
            partner.start()
        # On its own thread, so a slow or unreachable Telegram does not delay start-up.
        registration = None
        if deps.auto_register_webhook:
            registration = threading.Thread(target=register_at_start, args=(deps,), name="setuhaul-webhook", daemon=True)
            registration.start()
        try:
            # The application serves requests while suspended here.
            yield
        # On shutdown, stop the runner.
        finally:
            if runner is not None:
                await run_in_threadpool(runner.stop)
            if notices is not None:
                await run_in_threadpool(notices.stop)
            if partner is not None:
                await run_in_threadpool(partner.stop)
            if registration is not None:
                await run_in_threadpool(registration.join, 25)

    # A bearer name that looks like a web identity would let that token act as a signed-in user's claims.
    for token_name in deps.coordinator_tokens.values():
        if token_name.startswith(WEB_PREFIX):
            raise ValueError(f"coordinator token names may not start with {WEB_PREFIX!r}")

    # Interactive docs and the schema endpoint are switched off so the surface is exactly the routes below.
    app = FastAPI(title="SetuHaul rescheduling service", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    # Cookie-authenticated writes must come from our own pages. Registered first, so it runs inside the request-id step.
    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):  # type: ignore[no-untyped-def]
        if same_origin.is_cross_site(request, deps.public_url):
            await run_in_threadpool(same_origin.record_refusal, deps, request)
            return _reject(request.state.request_id, 403, "CROSS_SITE_REQUEST")
        return await call_next(request)

    # Runs around every request.
    @app.middleware("http")
    # Give every request an id (from the caller's x-request-id header or a new one) and echo it in the response.
    async def request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
        # Store the id for handlers.
        request.state.request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        # Run the handler.
        response = await call_next(request)
        # Echo the id so callers can quote it.
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    # Malformed request: a clean 422, not a framework error page.
    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        # INVALID_REQUEST, naming each field at fault; nothing that was sent is repeated.
        found = refusals.problems_from(exc.errors(), skip=1)
        return _reject(request.state.request_id, 422, "INVALID_REQUEST", **refusals.details(found))

    # Any unexpected error: a generic 500 with only the request id. No internal detail leaves the process.
    @app.exception_handler(Exception)
    async def internal(request: Request, exc: Exception) -> JSONResponse:
        # INTERNAL_ERROR.
        return JSONResponse({"status": "ERROR", "reason_code": "INTERNAL_ERROR", "request_id": getattr(request.state, "request_id", "")},
                            status_code=500)

    # ---- liveness and readiness ------------------------------------------------------------------------
    # Liveness: the process is up.
    @app.get("/health")
    async def health() -> dict[str, str]:
        # Nothing else is checked here.
        return {"status": "ok"}

    # The commit this build was made from (the image sets GIT_COMMIT); a deploy compares it with what it shipped.
    @app.get("/version")
    async def version() -> dict[str, str]:
        return {"commit": os.environ.get("GIT_COMMIT") or "unknown"}

    # Readiness: the database and cache can actually be used.
    @app.get("/ready")
    # The path the deploy standard asks every app for.
    @app.get("/health/ready")
    async def ready(request: Request) -> JSONResponse:
        # Returns a reason code, or None if ready.
        def check() -> str | None:
            try:
                # A read-only connection, so a readiness probe can never write.
                conn = open_connection(path=deps.db_path, readonly=True)
                try:
                    # The schema is present and migrated.
                    conn.execute("select 1 from schema_migrations limit 1").fetchone()
                finally:
                    conn.close()
            # Database missing or broken.
            except (sqlite3.Error, FileNotFoundError):
                return "DATABASE_UNAVAILABLE"
            try:
                # Is the cache reachable?
                deps.redis.ping()
            # Any cache failure means not ready.
            except Exception:  # noqa: BLE001 - any cache failure means not ready
                return "CACHE_UNAVAILABLE"
            # Everything checked out: no problem to report.
            return None

        # Run the blocking checks in a worker thread.
        problem = await run_in_threadpool(check)
        if problem:
            # Not ready: 503 with the reason.
            return JSONResponse({"ready": False, "reason_code": problem, "request_id": request.state.request_id}, status_code=503)
        # Ready.
        return JSONResponse({"ready": True, "request_id": request.state.request_id})

    # ---- driver messages --------------------------------------------------------------------------------
    # Handle one driver message: open a connection and run the pipeline, turning database failures into 503.
    def process(payload: MessageIn, request_id: str) -> JSONResponse:
        try:
            # A fresh connection per message (SQLite connections are not shared across threads).
            conn = open_connection(path=deps.db_path)
        except (sqlite3.Error, FileNotFoundError):
            # The database could not be opened.
            return _reject(request_id, 503, "DATABASE_UNAVAILABLE")
        try:
            # The real work.
            return _process(conn, payload, request_id)
        except sqlite3.Error:
            return _reject(request_id, 503, "DATABASE_UNAVAILABLE")
        finally:
            conn.close()

    # The ordered pipeline: sender lookup -> duplicate check -> rate limit -> claim the id -> conversation. All before the model is called.
    def _process(conn: sqlite3.Connection, payload: MessageIn, request_id: str) -> JSONResponse:
        try:
            # Identify the driver from their phone number. The model never supplies identity.
            trusted = resolve_eligible_driver(conn, payload.sender, deps.clock)
        except AuthError as exc:
            # Unknown or disallowed sender.
            return _reject(request_id, 403, exc.code)
        # The rest of the pipeline is shared with the Telegram channel, which resolves its driver differently.
        return _process_trusted(conn, trusted, payload, request_id)

    # Duplicate check -> rate limit -> claim the id -> conversation, for a driver whose identity is already verified.
    def _process_trusted(conn: sqlite3.Connection, trusted: TrustedDriverContext, payload: MessageIn, request_id: str) -> JSONResponse:
        # The clock.
        clock = deps.clock
        # The verified driver.
        driver_id = trusted.driver_id
        # A fingerprint of the message text: the same id with different text is a conflict.
        fp = hashlib.sha256(payload.text.encode()).hexdigest()
        # Seconds since 1970, for the processing timeout.
        now_epoch = int(clock.now().timestamp())

        def claim() -> JSONResponse | None:
            """None means we own the message; otherwise the response to return instead."""
            # One write transaction so two simultaneous deliveries cannot both pass.
            with immediate(conn):
                # Have we seen this message id from this driver?
                row = conn.execute("select * from inbound_messages where driver_id=? and message_id=?",
                                   (driver_id, payload.message_id)).fetchone()
                # New message: proceed (it is claimed below, after the rate limit).
                if row is None:
                    return None  # claimed below, after the rate limit
                # Same id, different text: reject as a mismatch.
                if row["fingerprint"] != fp:
                    return _reject(request_id, 409, "IDEMPOTENCY_MISMATCH")
                # Already processed: replay the stored reply with duplicate=true, without acting again.
                if row["status"] == "DONE":
                    body = json.loads(row["reply_json"])
                    return JSONResponse({**body, "duplicate": True, "request_id": request_id})
                # Still being processed by another worker: tell the caller to retry.
                if now_epoch - row["claimed_epoch"] < STALE_PROCESSING_SECONDS:
                    return _reject(request_id, 409, "IN_PROGRESS")
                # A stale claim (a crashed worker): clear it so this delivery can proceed.
                conn.execute("delete from inbound_messages where driver_id=? and message_id=?", (driver_id, payload.message_id))
                return None

        # Check for duplicates.
        early = claim()
        # A duplicate or conflict: answer now.
        if early is not None:
            return early
        # Per-driver rate limit, applied after the duplicate check so a replayed delivery does not use up budget.
        limit = check_rate_limit(deps.redis, driver_id, clock, deps.rate_limits, deps.channel)
        # Too many messages: 429 with Retry-After.
        if not limit.allowed:
            return _reject(request_id, 429, limit.reason_code or "RATE_LIMITED", retry_after_seconds=limit.retry_after_seconds)
        # Claim the message id. 'insert or ignore' plus the primary key means only one worker can succeed.
        with immediate(conn):
            cur = conn.execute(
                "insert or ignore into inbound_messages (driver_id, message_id, fingerprint, status, claimed_epoch, created_at) "
                "values (?,?,?, 'PROCESSING',?,?)", (driver_id, payload.message_id, fp, now_epoch, clock.now().isoformat()))
        # Another worker claimed it between our check and our insert.
        if cur.rowcount == 0:  # another worker claimed it between our check and our insert
            return _reject(request_id, 409, "IN_PROGRESS")

        # Collects this message's model calls.
        recorder = TurnRecorder(clock)
        try:
            # The driver's newest open conversation thread.
            row = conn.execute("select thread_id from chat_threads where driver_id=? and thread_status<>'CLOSED' "
                               "order by opened_at desc, rowid desc limit 1", (driver_id,)).fetchone()
            # Memory with live hold checks.
            memory = ConversationMemoryStore(conn, deps.redis, clock, load_settings(), hold_checker=make_hold_checker(conn, clock))
            # Use the open thread, or start one.
            thread_id = row["thread_id"] if row else memory.open_thread(driver_id)
            # A pass code is checked by the app. Everything after this line sees the message with any code blanked, so a code is
            # never saved, put in a case or sent to a model.
            clean = blank(payload.text)
            # Everything written from here is this turn's, to be tagged with the load it is about.
            before = chat_loads.mark(conn)
            lang = str(detect_language(clean.replace(HIDDEN, ""), memory.load(driver_id, thread_id).state.language).language)
            lang = lang if lang in LANGS else "en"
            answer = pass_check.check(conn, clock, driver_id=driver_id, thread_id=thread_id, text=payload.text, lang=lang,
                                      sink=SqlEscalationSink(conn, clock))
            if answer is not None:
                memory.append(driver_id, thread_id, "DRIVER", clean, external_message_id=payload.message_id)
                memory.append(driver_id, thread_id, "AGENT", answer.text)
                _record_turn(conn, memory, driver_id, thread_id, before, answer.shipment_id)
                body = {"status": "REPLIED", "reply": answer.text, "language": lang, "reason_code": None, "case_id": answer.case_id}
                with immediate(conn):
                    conn.execute("update inbound_messages set status='DONE', reply_json=?, completed_at=? where driver_id=? and message_id=?",
                                 (json.dumps(body), clock.now().isoformat(), driver_id, payload.message_id))
                return JSONResponse({**body, "duplicate": False, "request_id": request_id})
            # Bundle everything the conversation needs.
            gateway, policy = model_in_force(deps, conn)
            services = ConversationServices(
                conn=conn, memory=memory, gateway=recorder.wrap(gateway), policy=policy, sink=SqlEscalationSink(conn, clock),
                fallback_sink=JsonlEscalationSink(deps.fallback_path), exporter=deps.exporter,
                trace_env=env_in_force(deps, conn))
            # Run the conversation (this is where the model is called).
            reply = handle_message(InboundMessage(message_id=payload.message_id, text=clean),
                                   TrustedConversationContext(driver=trusted, thread_id=thread_id), clock, services)
            _record_turn(conn, memory, driver_id, thread_id, before)
            # The response body; this exact body is stored so a duplicate delivery gets the same answer.
            body = {"status": reply.status, "reply": reply.text, "language": reply.language, "reason_code": reply.escalation_reason,
                    "case_id": reply.case_id}
            with immediate(conn):
                # Mark the message processed and store its reply.
                conn.execute("update inbound_messages set status='DONE', reply_json=?, completed_at=? where driver_id=? and message_id=?",
                             (json.dumps(body), clock.now().isoformat(), driver_id, payload.message_id))
            # Saved after the reply is stored; it cannot fail the message.
            recorder.save(conn, reply.status, reply.escalation_reason)
            # First-time answer.
            return JSONResponse({**body, "duplicate": False, "request_id": request_id})
        # Anything went wrong: free the id so the channel's retry can succeed, then re-raise.
        except BaseException:
            with immediate(conn):  # free the id so the provider's retry can succeed
                conn.execute("delete from inbound_messages where driver_id=? and message_id=? and status='PROCESSING'",
                             (driver_id, payload.message_id))
            # The calls made before the failure still happened.
            recorder.save(conn, "ERROR", None)
            raise

    # The driver webhook.
    @app.post("/v1/driver/messages")
    async def driver_message(request: Request) -> JSONResponse:
        # This request's id.
        rid = request.state.request_id
        # The raw bytes: the signature is checked over these BEFORE any parsing.
        raw = await request.body()
        try:
            # Verify the HMAC signature.
            verify_channel_request(raw, request.headers.get("x-signature", ""), deps.webhook_secret)
        except AuthError as exc:
            # Bad or missing signature: 401. Nothing in the body has been parsed yet.
            return _reject(rid, 401, exc.code)  # nothing in the body has been parsed yet
        try:
            # Only now parse and validate the body.
            payload = MessageIn.model_validate(json.loads(raw))
        except (ValueError, ValidationError):
            # Malformed body.
            return _reject(rid, 422, "INVALID_REQUEST")
        # Run the pipeline in a worker thread.
        return await run_in_threadpool(process, payload, rid)

    # ---- GPS pings --------------------------------------------------------------------------------------
    # Shared by both GPS routes: verify the signature, parse, then store in one committed write transaction.
    # `store` runs on a worker thread with the open connection and the parsed body; it returns a ping id or a rejection response.
    async def ingest_ping(request: Request, model: type[GpsPing],
                          store: Callable[[sqlite3.Connection, Any, str], int | JSONResponse]) -> JSONResponse:
        # This request's id.
        rid = request.state.request_id
        # The raw bytes: the signature is checked over these BEFORE any parsing.
        raw = await request.body()
        try:
            # Verify the HMAC signature.
            verify_channel_request(raw, request.headers.get("x-signature", ""), deps.webhook_secret)
        except AuthError as exc:
            # Bad or missing signature: 401, nothing parsed yet.
            return _reject(rid, 401, exc.code)
        try:
            # Only now parse and validate the body (coordinate range and aware time are enforced by GpsPing).
            payload = model.model_validate(json.loads(raw))
        except (ValueError, ValidationError):
            # Malformed body: a bare 422, with no values echoed back.
            return _reject(rid, 422, "INVALID_REQUEST")
        # A fix stamped far in the future (a skewed device clock) would shadow every real ping after it: refuse it.
        if payload.recorded_at > deps.clock.now() + timedelta(seconds=GPS_MAX_FUTURE_SECONDS):
            return _reject(rid, 422, "INVALID_REQUEST")

        # Blocking database work, run in a worker thread.
        def work() -> JSONResponse:
            try:
                # A fresh connection per request (SQLite connections are not shared across threads).
                conn = open_connection(path=deps.db_path)
            except (sqlite3.Error, FileNotFoundError):
                # The database could not be opened.
                return _reject(rid, 503, "DATABASE_UNAVAILABLE")
            try:
                # One write transaction: committed on success, rolled back on error.
                with immediate(conn):
                    result = store(conn, payload, rid)
                # A rejection response from the store callback is passed straight through.
                if isinstance(result, JSONResponse):
                    return result
                # The truck has a new position: tell the driver, release a hold that is now out of reach, and offer earlier slots if
                # the truck is much closer. The ping is already stored, so a failure here must not turn the ingest into an error.
                try:
                    row = conn.execute("select p.shipment_id, p.source, s.driver_id from gps_pings p join shipments s using(shipment_id) "
                                       "where p.ping_id=?", (result,)).fetchone()
                    if row is not None:
                        memory = ConversationMemoryStore(conn, deps.redis, deps.clock, load_settings(),
                                                         hold_checker=make_hold_checker(conn, deps.clock))
                        if row["source"] == "DRIVER_SHARED":
                            # A driver's own position applies to all of their open loads.
                            handle_driver_position(conn, row["driver_id"], deps.clock, memory)
                        else:
                            # A telematics ping names one shipment.
                            handle_new_position(conn, row["shipment_id"], deps.clock, memory)
                except Exception:  # noqa: BLE001, S110 - the ping is stored; reassessment is best effort and retried on the next ping
                    pass
                # Success: report the new ping id.
                return JSONResponse({"status": "STORED", "ping_id": result, "request_id": rid})
            except UnknownShipment:
                # No such shipment (the id is not echoed).
                return _reject(rid, 404, "UNKNOWN_SHIPMENT")
            except sqlite3.Error:
                return _reject(rid, 503, "DATABASE_UNAVAILABLE")
            finally:
                conn.close()

        # Run it off the event loop.
        return await run_in_threadpool(work)

    # Telematics feed: a signed ping for a named shipment.
    @app.post("/v1/gps/pings")
    async def gps_ping(request: Request) -> JSONResponse:
        # Store the ping against the shipment named in the body.
        def store(conn: sqlite3.Connection, p: TelematicsPingIn, rid: str) -> int:
            return store_ping(conn, p.shipment_id, GpsPing(lat=p.lat, lon=p.lon, recorded_at=p.recorded_at), deps.clock, "TELEMATICS")

        return await ingest_ping(request, TelematicsPingIn, store)

    # Chat-channel adapter: a driver shares their location; it is stored against their active shipment.
    @app.post("/v1/driver/location")
    async def driver_location(request: Request) -> JSONResponse:
        # Identify the driver, find their open shipment, store the ping.
        def store(conn: sqlite3.Connection, p: DriverLocationIn, rid: str) -> int | JSONResponse:
            try:
                # Identity comes from the phone number, never from the body's content.
                driver = resolve_eligible_driver(conn, p.sender, deps.clock)
            except AuthError as exc:
                return _reject(rid, 403, exc.code)
            # The shipment being driven now.
            shipment_id = active_shipment_for_driver(conn, driver.driver_id)
            if shipment_id is None:
                # Nothing active: nothing stored.
                return _reject(rid, 404, "NO_ACTIVE_SHIPMENT")
            return store_ping(conn, shipment_id, GpsPing(lat=p.lat, lon=p.lon, recorded_at=p.recorded_at), deps.clock, "DRIVER_SHARED")

        return await ingest_ping(request, DriverLocationIn, store)

    # ---- driver status ----------------------------------------------------------------------------------
    # Read a driver's case status and recent exceptions (read-only).
    def driver_status(phone: str, rid: str) -> JSONResponse:
        try:
            # Read-only: this endpoint can never change data.
            conn = open_connection(path=deps.db_path, readonly=True)
        except (sqlite3.Error, FileNotFoundError):
            return _reject(rid, 503, "DATABASE_UNAVAILABLE")
        try:
            try:
                # Identify the driver by phone.
                driver = resolve_eligible_driver(conn, phone, deps.clock)
            except AuthError as exc:
                return _reject(rid, 403, exc.code)
            # Their current hand-off case, if any.
            case = get_driver_case_status(conn, driver.driver_id)
            # Their ten most recent exceptions.
            exceptions = [{"exception_id": r["exception_id"], "shipment_id": r["shipment_id"], "type": r["exception_type"],
                           "status": r["exception_status"], "reported_at": r["reported_at"]}
                          for r in conn.execute("select * from driver_exceptions where driver_id=? order by reported_at desc limit 10",
                                                (driver.driver_id,))]
            return JSONResponse({"case": case.model_dump(mode="json") if case else None, "exceptions": exceptions, "request_id": rid})
        except sqlite3.Error:
            return _reject(rid, 503, "DATABASE_UNAVAILABLE")
        finally:
            conn.close()

    # Signed status endpoint.
    @app.get("/v1/driver/status")
    async def driver_status_endpoint(request: Request, phone: str, ts: int) -> JSONResponse:
        rid = request.state.request_id
        # Reject requests whose timestamp is more than 5 minutes off, so a captured request cannot be replayed later.
        if abs(int(deps.clock.now().timestamp()) - ts) > SIGNATURE_WINDOW_SECONDS:
            return _reject(rid, 401, "STALE_REQUEST")
        try:
            # The signature covers the phone number and the timestamp.
            verify_channel_request(f"{phone}|{ts}".encode(), request.headers.get("x-signature", ""), deps.webhook_secret)
        except AuthError as exc:
            return _reject(rid, 401, exc.code)
        return await run_in_threadpool(driver_status, phone, rid)

    # ---- coordinator ------------------------------------------------------------------------------------
    # Identify a coordinator from a bearer token.
    def coordinator(request: Request) -> str | None:
        # The Authorization header.
        header = request.headers.get("authorization", "")
        # Strip 'Bearer '.
        token = header[7:] if header.lower().startswith("bearer ") else ""
        # Compare against every known token in constant time.
        for known, name in deps.coordinator_tokens.items():
            if hmac.compare_digest(known, token):
                return name
        # No token matched: not a coordinator.
        return None

    # Run a function with a fresh connection, turning database failures into 503.
    def with_conn(rid: str, fn: Callable[[sqlite3.Connection], JSONResponse]) -> JSONResponse:
        try:
            conn = open_connection(path=deps.db_path)
        except (sqlite3.Error, FileNotFoundError):
            return _reject(rid, 503, "DATABASE_UNAVAILABLE")
        try:
            return fn(conn)
        except sqlite3.Error:
            return _reject(rid, 503, "DATABASE_UNAVAILABLE")
        finally:
            conn.close()

    # Turn an escalation transition result into an HTTP response.
    def transition_response(rid: str, r: TransitionResult) -> JSONResponse:
        # The successful outcomes.
        ok = {"CLAIMED", "RESOLVED", "REOPENED"}
        if r.status in ok or (r.status == "ALREADY_CLAIMED" and r.replayed):
            return JSONResponse({"status": "CLAIMED" if r.status == "ALREADY_CLAIMED" else r.status, "case_id": r.case_id, "request_id": rid})
        # Map refusals to status codes: 404 not found, 403 not your case, 422 bad resolution, otherwise 409 conflict.
        code = {"NOT_FOUND": 404, "NOT_YOUR_CASE": 403, "INVALID_RESOLUTION": 422}.get(r.status, 409)
        return _reject(rid, code, r.status, case_id=r.case_id, claimed_by=r.claimed_by)

    # Who is calling a case route: a legacy bearer token (all cases, the token's name) or a web session (scoped, the user's name).
    # A plain dependency, so it runs (in a worker thread) before the body is validated and an anonymous caller always gets 401.
    def case_actor(request: Request) -> tuple[str, User | None]:
        # The service-to-service fallback keeps its old meaning exactly.
        who = coordinator(request)
        if who is not None:
            return who, None
        # Neither a bearer token nor a session cookie: the same 401 as before.
        if not request.cookies.get(sessions.COOKIE_NAME):
            raise AuthzError(401, "UNAUTHORIZED")
        # A session must carry a warehouse role with a facility (or be an admin); refusals are 401/403/503.
        user = require_role("WAREHOUSE_AUTHORITY", "ADMIN")(request)
        # The stored identity is the unique e-mail under a prefix no bearer name may use, so it can never equal anyone else's.
        return WEB_PREFIX + user.email, user

    # A case that is not in the session user's scope looks like it does not exist.
    def not_found(rid: str, case_id: str) -> JSONResponse:
        return _reject(rid, 404, "NOT_FOUND", case_id=case_id, claimed_by=None)

    # Run a change on one case after the scope check (the bearer fallback has no scope).
    def change_case(rid: str, case_id: str, user: User | None, act: Callable[[sqlite3.Connection], TransitionResult]) -> JSONResponse:
        def go(c: sqlite3.Connection) -> JSONResponse:
            if user is not None and not case_ids_in_scope(c, user, case_id=case_id):
                return not_found(rid, case_id)
            return transition_response(rid, act(c))

        return with_conn(rid, go)

    # List cases (open and claimed by default).
    @app.get("/v1/coordinator/cases")
    async def cases(request: Request, actor: tuple[str, User | None] = Depends(case_actor), status: str | None = None, limit: int = 50) -> JSONResponse:
        rid = request.state.request_id
        user = actor[1]
        # Keep only valid status names from the query.
        statuses = tuple(s for s in (status or "OPEN,CLAIMED").split(",") if s in ("OPEN", "CLAIMED", "RESOLVED", "CANCELLED"))
        statuses = statuses or ("OPEN", "CLAIMED")

        def read(c: sqlite3.Connection) -> JSONResponse:
            if user is None:
                found = list_open_cases(c, statuses, min(limit, 200))
            else:
                # Same order and cap as the unscoped list, but only cases bound for the user's facilities.
                found = [x for x in (get_case(c, i) for i in case_ids_in_scope(c, user, statuses, min(limit, 200))) if x is not None]
            return JSONResponse({"cases": [{**x.model_dump(mode="json"), "claimed_by_label": claim_label(c, x.claimed_by)} for x in found],
                                 "request_id": rid})

        return await run_in_threadpool(with_conn, rid, read)

    # One case with its full history.
    @app.get("/v1/coordinator/cases/{case_id}")
    async def case_detail(request: Request, case_id: str, actor: tuple[str, User | None] = Depends(case_actor)) -> JSONResponse:
        rid = request.state.request_id
        user = actor[1]

        # Load the case and its events.
        def read(c: sqlite3.Connection) -> JSONResponse:
            case = get_case(c, case_id)
            if case is None or (user is not None and not case_ids_in_scope(c, user, case_id=case_id)):
                return _reject(rid, 404, "NOT_FOUND")
            from setuhaul.escalation import case_events

            return JSONResponse({"case": {**case.model_dump(mode="json"), "claimed_by_label": claim_label(c, case.claimed_by)},
                                 "events": case_events(c, case_id), "request_id": rid})

        return await run_in_threadpool(with_conn, rid, read)

    # Claim a case.
    @app.post("/v1/coordinator/cases/{case_id}/claim")
    async def claim_endpoint(request: Request, case_id: str, actor: tuple[str, User | None] = Depends(case_actor)) -> JSONResponse:
        rid = request.state.request_id
        who, user = actor
        return await run_in_threadpool(change_case, rid, case_id, user, lambda c: claim_case(c, case_id, who, deps.clock))

    # Resolve a claimed case.
    @app.post("/v1/coordinator/cases/{case_id}/resolve")
    async def resolve_endpoint(request: Request, case_id: str, body: ResolveIn, actor: tuple[str, User | None] = Depends(case_actor)) -> JSONResponse:
        rid = request.state.request_id
        who, user = actor
        def refused(code: str, status: int = 422, message: str | None = None) -> JSONResponse:
            return JSONResponse({"status": "REJECTED", "reason_code": code, "message": message or assign.MESSAGES[code],
                                 "request_id": rid}, status_code=status)

        # A slot is booked in a person's name, so only a web session can send one; its "assigned a slot" must name it.
        if body.slot_id is not None and (user is None or body.resolution_code != "ASSIGNED_SLOT"):
            return refused("SLOT_NOT_ALLOWED")
        if user is not None and body.resolution_code == "ASSIGNED_SLOT" and body.slot_id is None:
            return refused("SLOT_REQUIRED")

        def work(c: sqlite3.Connection) -> TransitionResult:
            apply = None
            slot_id = body.slot_id
            if user is not None and slot_id is not None:
                def apply(case: sqlite3.Row) -> str:
                    dock, start = assign.assign_in_txn(c, actor=user, shipment_id=case["shipment_id"], slot_id=slot_id,
                                                       clock=deps.clock)
                    language = str(json.loads(case["context_json"]).get("language") or "en")
                    return say("booking_assigned", language, dock=dock, date=f"{start:%d %b %Y}", start=f"{start:%H:%M}")
            return resolve_case(c, case_id, who, body.resolution_code, body.note, deps.clock, apply=apply)

        def go() -> JSONResponse:
            try:
                return change_case(rid, case_id, user, work)
            except AuthError as exc:
                if exc.code not in assign.MESSAGES:
                    raise
                return refused(exc.code, assign.status_of(exc.code), assign.message_of(exc))

        return await run_in_threadpool(go)

    # Reopen a recently resolved case.
    @app.post("/v1/coordinator/cases/{case_id}/reopen")
    async def reopen_endpoint(request: Request, case_id: str, actor: tuple[str, User | None] = Depends(case_actor)) -> JSONResponse:
        rid = request.state.request_id
        who, user = actor
        return await run_in_threadpool(change_case, rid, case_id, user, lambda c: reopen_case(c, case_id, who, deps.clock))

    # Google sign-in routes.
    from setuhaul.auth.routes import register_auth_routes

    register_auth_routes(app, deps)
    # Email and password sign-in.
    from setuhaul.auth.password_routes import register_password_routes

    register_password_routes(app, deps)
    # Driver onboarding and approval.
    from setuhaul.auth.admin_routes import register_admin_routes

    register_admin_routes(app, deps)
    # Read APIs for the warehouse screens: partner slot requests and project drivers' chats.
    from setuhaul.auth.warehouse_routes import register_warehouse_routes

    register_warehouse_routes(app, deps)
    from setuhaul.auth.shipment_routes import register_shipment_routes

    register_shipment_routes(app, deps)
    from setuhaul.auth.exception_routes import register_exception_routes

    register_exception_routes(app, deps)
    from setuhaul.auth.gate_routes import register_gate_routes

    register_gate_routes(app, deps)
    # Admin JSON APIs: users and the audit log.
    from setuhaul.auth.admin_api import register_admin_api

    register_admin_api(app, deps)
    # Settings the admin may change: read, save, reset.
    from setuhaul.auth.settings_routes import register_settings_routes

    register_settings_routes(app, deps)
    # The sign-in landing page and the admin and warehouse screens.
    from setuhaul.auth.pages import register_pages

    register_pages(app, deps)
    # The signed-out password screens.
    from setuhaul.auth.password_pages import register_password_pages

    register_password_pages(app, deps)
    # Facility change requests: list, approve, reject.
    from setuhaul.auth.facility_routes import register_facility_routes

    register_facility_routes(app, deps)
    # The Telegram driver channel reuses the driver pipeline above.
    from setuhaul.auth.telegram_routes import register_telegram_routes

    register_telegram_routes(app, deps, _process_trusted)
    # The demo screen is registered only when a demo state is supplied.
    if deps.demo is not None:
        from setuhaul.demo_ui import register_demo

        register_demo(app, deps, process)
    return app


def telegram_from_env() -> tuple[bytes, TelegramClient | None]:
    """The Telegram webhook secret and reply client from the environment; (b"", None) leaves the route off."""
    import logging

    secret = os.environ.get("SETUHAUL_TELEGRAM_WEBHOOK_SECRET", "")
    token = os.environ.get("SETUHAUL_TELEGRAM_BOT_TOKEN", "")
    if not (secret and token):
        return b"", None
    if len(secret) < MIN_TELEGRAM_SECRET_LEN:
        # The values themselves are never logged.
        logging.getLogger(__name__).warning(
            "Telegram disabled: SETUHAUL_TELEGRAM_WEBHOOK_SECRET must be at least %d characters", MIN_TELEGRAM_SECRET_LEN)
        return b"", None
    import httpx

    from setuhaul.auth.telegram import RealTelegramClient

    return secret.encode(), RealTelegramClient(token, httpx.Client(timeout=10.0))


def partner_mail_from_env() -> tuple[tuple[MailSender, MailReader] | None, bool, str]:
    """Partner-warehouse mail transport for live mode only. Misconfiguration logs a warning and leaves the runner off."""
    try:
        mail_settings = load_mail_settings()
    except ValueError as exc:
        logging.getLogger(__name__).warning("Partner email runner disabled: %s", exc)
        return None, False, "mx.google.com"
    if mail_settings.mode != "live":
        return None, False, mail_settings.auth_serv_id
    return build_mail(mail_settings), True, mail_settings.auth_serv_id


def create_app_from_env() -> FastAPI:
    """``uvicorn --factory setuhaul.api:create_app_from_env``. Secrets come from the environment only."""

    # Central settings.
    configure_logging()
    s = load_settings()
    # Stop before anything opens the sample database: it is the pristine source that working copies are made from.
    problem = sample_database_problem(s.db_path)
    if problem:
        raise SystemExit(problem)
    # Every warehouse is open 24 hours; those with a crew number get their slots topped up. Imported here: it uses auth.
    from setuhaul import round_clock

    # A database that is missing or not yet migrated is reported by the readiness check, not by refusing to start.
    try:
        boot = open_connection(path=s.db_path)
        try:
            round_clock.open_all_hours(boot)
            round_clock.top_up(boot, SystemClock())
        finally:
            boot.close()
    except (sqlite3.Error, FileNotFoundError):
        logging.getLogger(__name__).warning("Round-the-clock hours and slots not applied", exc_info=True)
    # Coordinator tokens as a JSON map in an environment variable.
    tokens = json.loads(os.environ.get("SETUHAUL_COORDINATOR_TOKENS", "{}"))
    from setuhaul.model_gateway import PROVIDERS, build_gateway, load_model_policy

    # Model limits and provider from the environment.
    policy = load_model_policy()
    # Builds the model gateway.
    factory: Callable[[], ModelGateway]
    # Load tests and demos only: the rule-based stand-in.
    if os.environ.get("SETUHAUL_USE_STUB_MODEL") == "1":  # load tests and demos only
        from setuhaul.stub_model import StubGateway

        factory = StubGateway
    # A configured provider: build its adapter once and reuse it.
    elif policy.provider in PROVIDERS:
        gw = build_gateway(policy)
        factory = lambda: gw
    else:
        # No provider: every turn takes the safe outage path.
        factory = UnconfiguredGateway
    # Load tests only: in-process fake Redis, so run a single worker.
    if s.redis_url.startswith("fakeredis"):  # load tests only: in-process, so run a single worker
        import fakeredis

        redis: Redis = fakeredis.FakeRedis(decode_responses=True)
    else:
        # The real Redis.
        redis = Redis.from_url(s.redis_url, decode_responses=True, socket_connect_timeout=1.0, socket_timeout=1.0)
    # The real clock by default.
    clock: Clock = SystemClock()
    # Load tests against the dated snapshot only: a fixed time.
    if os.environ.get("SETUHAUL_FAKE_NOW"):  # load tests against the dated snapshot only
        from setuhaul.clock import FakeClock

        clock = FakeClock(parse_operational_timestamp(os.environ["SETUHAUL_FAKE_NOW"]))
    # Google sign-in is configured only when the client id and secret are both present.
    google: GoogleVerifier | None = None
    if os.environ.get("SETUHAUL_GOOGLE_CLIENT_ID") and os.environ.get("SETUHAUL_GOOGLE_CLIENT_SECRET"):
        import httpx

        from setuhaul.auth.google import RealGoogleVerifier

        google = RealGoogleVerifier(os.environ["SETUHAUL_GOOGLE_CLIENT_ID"], os.environ["SETUHAUL_GOOGLE_CLIENT_SECRET"],
                                    httpx.Client(timeout=10.0), clock)
    # The first admin, created at start-up when an address is configured (idempotent).
    if os.environ.get("SETUHAUL_BOOTSTRAP_ADMIN_EMAIL"):
        from setuhaul.auth import users

        boot = open_connection(path=s.db_path)
        try:
            users.bootstrap_admin(boot, os.environ["SETUHAUL_BOOTSTRAP_ADMIN_EMAIL"], clock)
        finally:
            boot.close()
    # Telegram: the webhook secret and, when both are usable, the client that sends replies.
    tg_secret, tg_client = telegram_from_env()
    # Email and password sign-in: the mailbox may also be saved from the screen, so this only warns about .env.
    _, unset = account_sender_from_env(os.environ)
    if unset:
        logging.getLogger(__name__).warning("Password sign-in disabled: set %s", ", ".join(unset))
    # Partner-warehouse email: runs automatically in the background only when configured for live use.
    p_mail, p_require_auth, p_serv_id = partner_mail_from_env()
    # Assemble the dependencies and build the app.
    return create_app(AppDeps(
        telegram_secret=tg_secret, telegram=tg_client,
        settings=SettingsSource(os.environ, stub=os.environ.get("SETUHAUL_USE_STUB_MODEL") == "1"),
        google=google, session_secret=os.environ.get("SETUHAUL_SESSION_SECRET", "").encode(),
        local_admin_enabled=os.environ.get("SETUHAUL_LOCAL_ADMIN") == "1",
        public_url=os.environ.get("SETUHAUL_PUBLIC_URL", ""),
        auto_register_webhook=os.environ.get("SETUHAUL_TELEGRAM_AUTO_REGISTER") == "1",
        db_path=s.db_path, redis=redis,
        webhook_secret=os.environ.get("SETUHAUL_WEBHOOK_SECRET", "").encode(), coordinator_tokens=tokens,
        fallback_path=Path(os.environ.get("SETUHAUL_FALLBACK_ESCALATIONS", "escalations-fallback.jsonl")), policy=policy,
        # With a bot, queued notices are sent, and the sweep runs so that lapsed offers are noticed and unbooked moves go back.
        gateway_factory=factory, run_driver_notices=tg_client is not None,
        partner_mail=p_mail, run_partner_mail=p_mail is not None,
        partner_mail_require_auth=p_require_auth, partner_mail_auth_serv_id=p_serv_id,
        run_maintenance=os.environ.get("SETUHAUL_RUN_MAINTENANCE") == "1" or tg_client is not None, clock=clock))


# Public names (some are re-exported for tests).
__all__ = ["IST", "AppDeps", "UnconfiguredGateway", "create_app", "create_app_from_env", "parse_operational_timestamp", "timedelta"]
