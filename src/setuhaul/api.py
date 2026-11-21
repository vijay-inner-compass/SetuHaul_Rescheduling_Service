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

# os reads environment configuration.
import os

# sqlite3 for connections and database errors.
import sqlite3

# uuid makes request ids.
import uuid

# Typing helpers.
from collections.abc import AsyncIterator, Callable, Sequence

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
from fastapi import FastAPI, Request

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

# Live hold checker for conversation memory.
from setuhaul.booking import make_hold_checker

# Real or simulated clock.
from setuhaul.clock import Clock, SystemClock

# Settings.
from setuhaul.config import load_settings

# The conversation workflow.
from setuhaul.conversation import (
    ConversationServices,
    InboundMessage,
    TrustedConversationContext,
    handle_message,
)

# Database helpers.
from setuhaul.db import IST, immediate, open_connection, parse_operational_timestamp

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
from setuhaul.identity import AuthError, resolve_driver, verify_channel_request

# The background maintenance runner.
from setuhaul.jobs.expire_holds import MaintenanceRunner

# Conversation memory.
from setuhaul.memory import ConversationMemoryStore

# Model types.
from setuhaul.model_gateway import (
    ModelGateway,
    ModelMessage,
    ModelPolicy,
    ModelResponse,
    ModelUnavailable,
)

# Span exporter type.
from setuhaul.observability import InMemoryExporter

# Per-driver rate limiting.
# A new truck position: confirmation, hold release and earlier-slot offers.
from setuhaul.position import handle_driver_position, handle_new_position

# Per-driver rate limiting.
from setuhaul.rate_limit import RateLimits, check_rate_limit

# The validated ping shape.
from setuhaul.schemas import GpsPing

# A message stuck in PROCESSING for this long is presumed abandoned and may be reclaimed.
STALE_PROCESSING_SECONDS = 60
# A signed status request older than this (5 minutes) is rejected, so a captured request cannot be replayed later.
SIGNATURE_WINDOW_SECONDS = 300
# A GPS ping may be stamped at most this far (5 minutes) ahead of the server clock; allows small device clock drift only.
GPS_MAX_FUTURE_SECONDS = 300


class UnconfiguredGateway:
    """Used when no provider is set: every turn takes the safe model-outage path."""

    # Always fails with MODEL_NOT_CONFIGURED, which sends the turn down the safe outage path.
    def complete(self, messages: Sequence[ModelMessage], tools: Sequence[dict[str, Any]], policy: ModelPolicy) -> ModelResponse:
        raise ModelUnavailable("MODEL_NOT_CONFIGURED")


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


# The inbound webhook body. Unknown fields are rejected (extra='forbid').
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


# A structured error body: status, reason code and request id. No stack trace, SQL or path ever appears.
def _reject(request_id: str, status: int, reason: str, **extra: Any) -> JSONResponse:
    # Rate-limit rejections tell the client when to retry.
    headers = {"Retry-After": str(extra["retry_after_seconds"])} if "retry_after_seconds" in extra else None
    # The rejection response.
    return JSONResponse({"status": "REJECTED", "reason_code": reason, "request_id": request_id, **extra}, status_code=status,
                        headers=headers)


# Build the FastAPI application from its dependencies.
def create_app(deps: AppDeps) -> FastAPI:
    # Start-up and shut-down hook.
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # No background runner unless enabled.
        runner = None
        # Start the hold-expiry sweep in a background thread.
        if deps.run_maintenance:
            # The runner opens its own connection (SQLite connections cannot cross threads).
            runner = MaintenanceRunner(None, deps.clock, deps.maintenance_interval_s,
                                       conn_factory=lambda: open_connection(path=deps.db_path))
            # Start it off the event loop.
            await run_in_threadpool(runner.start)
        try:
            # The application serves requests while suspended here.
            yield
        # On shutdown, stop the runner.
        finally:
            if runner is not None:
                await run_in_threadpool(runner.stop)

    # Interactive docs and the schema endpoint are switched off so the surface is exactly the routes below.
    app = FastAPI(title="SetuHaul rescheduling service", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

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
        # INVALID_REQUEST.
        return _reject(request.state.request_id, 422, "INVALID_REQUEST")

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

    # Readiness: the database and cache can actually be used.
    @app.get("/ready")
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
        # The clock.
        clock = deps.clock
        try:
            # Identify the driver from their phone number. The model never supplies identity.
            trusted = resolve_driver(conn, payload.sender)
        except AuthError as exc:
            # Unknown or disallowed sender.
            return _reject(request_id, 403, exc.code)
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

        try:
            # The driver's newest open conversation thread.
            row = conn.execute("select thread_id from chat_threads where driver_id=? and thread_status<>'CLOSED' "
                               "order by opened_at desc, rowid desc limit 1", (driver_id,)).fetchone()
            # Memory with live hold checks.
            memory = ConversationMemoryStore(conn, deps.redis, clock, load_settings(), hold_checker=make_hold_checker(conn, clock))
            # Use the open thread, or start one.
            thread_id = row["thread_id"] if row else memory.open_thread(driver_id)
            # Bundle everything the conversation needs.
            services = ConversationServices(
                conn=conn, memory=memory, gateway=deps.gateway_factory(), policy=deps.policy, sink=SqlEscalationSink(conn, clock),
                fallback_sink=JsonlEscalationSink(deps.fallback_path), exporter=deps.exporter)
            # Run the conversation (this is where the model is called).
            reply = handle_message(InboundMessage(message_id=payload.message_id, text=payload.text),
                                   TrustedConversationContext(driver=trusted, thread_id=thread_id), clock, services)
            # The response body; this exact body is stored so a duplicate delivery gets the same answer.
            body = {"status": reply.status, "reply": reply.text, "language": reply.language, "reason_code": reply.escalation_reason,
                    "case_id": reply.case_id}
            with immediate(conn):
                # Mark the message processed and store its reply.
                conn.execute("update inbound_messages set status='DONE', reply_json=?, completed_at=? where driver_id=? and message_id=?",
                             (json.dumps(body), clock.now().isoformat(), driver_id, payload.message_id))
            # First-time answer.
            return JSONResponse({**body, "duplicate": False, "request_id": request_id})
        # Anything went wrong: free the id so the channel's retry can succeed, then re-raise.
        except BaseException:
            with immediate(conn):  # free the id so the provider's retry can succeed
                conn.execute("delete from inbound_messages where driver_id=? and message_id=? and status='PROCESSING'",
                             (driver_id, payload.message_id))
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
                driver = resolve_driver(conn, p.sender)
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
                driver = resolve_driver(conn, phone)
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

    # List cases (open and claimed by default).
    @app.get("/v1/coordinator/cases")
    async def cases(request: Request, status: str | None = None, limit: int = 50) -> JSONResponse:
        rid = request.state.request_id
        if coordinator(request) is None:
            return _reject(rid, 401, "UNAUTHORIZED")
        # Keep only valid status names from the query.
        statuses = tuple(s for s in (status or "OPEN,CLAIMED").split(",") if s in ("OPEN", "CLAIMED", "RESOLVED", "CANCELLED"))
        return await run_in_threadpool(with_conn, rid, lambda c: JSONResponse(
            {"cases": [x.model_dump(mode="json") for x in list_open_cases(c, statuses or ("OPEN", "CLAIMED"), min(limit, 200))], "request_id": rid}))

    # One case with its full history.
    @app.get("/v1/coordinator/cases/{case_id}")
    async def case_detail(request: Request, case_id: str) -> JSONResponse:
        rid = request.state.request_id
        if coordinator(request) is None:
            return _reject(rid, 401, "UNAUTHORIZED")

        # Load the case and its events.
        def read(c: sqlite3.Connection) -> JSONResponse:
            case = get_case(c, case_id)
            if case is None:
                return _reject(rid, 404, "NOT_FOUND")
            from setuhaul.escalation import case_events

            return JSONResponse({"case": case.model_dump(mode="json"), "events": case_events(c, case_id), "request_id": rid})

        return await run_in_threadpool(with_conn, rid, read)

    # Claim a case.
    @app.post("/v1/coordinator/cases/{case_id}/claim")
    async def claim_endpoint(request: Request, case_id: str) -> JSONResponse:
        rid, who = request.state.request_id, coordinator(request)
        if who is None:
            return _reject(rid, 401, "UNAUTHORIZED")
        return await run_in_threadpool(with_conn, rid, lambda c: transition_response(rid, claim_case(c, case_id, who, deps.clock)))

    # Resolve a claimed case.
    @app.post("/v1/coordinator/cases/{case_id}/resolve")
    async def resolve_endpoint(request: Request, case_id: str, body: ResolveIn) -> JSONResponse:
        rid, who = request.state.request_id, coordinator(request)
        if who is None:
            return _reject(rid, 401, "UNAUTHORIZED")
        return await run_in_threadpool(with_conn, rid, lambda c: transition_response(
            rid, resolve_case(c, case_id, who, body.resolution_code, body.note, deps.clock)))

    # Reopen a recently resolved case.
    @app.post("/v1/coordinator/cases/{case_id}/reopen")
    async def reopen_endpoint(request: Request, case_id: str) -> JSONResponse:
        rid, who = request.state.request_id, coordinator(request)
        if who is None:
            return _reject(rid, 401, "UNAUTHORIZED")
        return await run_in_threadpool(with_conn, rid, lambda c: transition_response(rid, reopen_case(c, case_id, who, deps.clock)))

    # The demo screen is registered only when a demo state is supplied.
    if deps.demo is not None:
        from setuhaul.demo_ui import register_demo

        register_demo(app, deps, process)
    return app


def create_app_from_env() -> FastAPI:
    """``uvicorn --factory setuhaul.api:create_app_from_env``. Secrets come from the environment only."""
    # Central settings.
    s = load_settings()
    # Coordinator tokens as a JSON map in an environment variable.
    tokens = json.loads(os.environ.get("SETUHAUL_COORDINATOR_TOKENS", "{}"))
    from setuhaul.model_gateway import build_gateway, load_model_policy

    # Model limits and provider from the environment.
    policy = load_model_policy()
    # Builds the model gateway.
    factory: Callable[[], ModelGateway]
    # Load tests and demos only: the rule-based stand-in.
    if os.environ.get("SETUHAUL_USE_STUB_MODEL") == "1":  # load tests and demos only
        from setuhaul.stub_model import StubGateway

        factory = StubGateway
    # A configured provider: build its adapter once and reuse it.
    elif policy.provider:
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
        redis = Redis.from_url(s.redis_url, decode_responses=True)
    # The real clock by default.
    clock: Clock = SystemClock()
    # Load tests against the dated snapshot only: a fixed time.
    if os.environ.get("SETUHAUL_FAKE_NOW"):  # load tests against the dated snapshot only
        from setuhaul.clock import FakeClock

        clock = FakeClock(parse_operational_timestamp(os.environ["SETUHAUL_FAKE_NOW"]))
    # Assemble the dependencies and build the app.
    return create_app(AppDeps(
        db_path=s.db_path, redis=redis,
        webhook_secret=os.environ.get("SETUHAUL_WEBHOOK_SECRET", "").encode(), coordinator_tokens=tokens,
        fallback_path=Path(os.environ.get("SETUHAUL_FALLBACK_ESCALATIONS", "escalations-fallback.jsonl")), policy=policy,
        gateway_factory=factory, run_maintenance=os.environ.get("SETUHAUL_RUN_MAINTENANCE") == "1", clock=clock))


# Public names (some are re-exported for tests).
__all__ = ["IST", "AppDeps", "create_app", "create_app_from_env", "parse_operational_timestamp", "timedelta"]
