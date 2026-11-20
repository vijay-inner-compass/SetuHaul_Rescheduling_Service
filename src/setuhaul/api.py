"""HTTP surface: driver webhook, driver status, coordinator queue, /health and /ready.

Order of work on a driver message, cheapest and safest first, always before the model is invoked:
signature (raw bytes, before any parsing) -> sender lookup -> duplicate delivery -> rate limit ->
claim the message id -> conversation. Errors are structured ({status, reason_code, request_id});
no stack trace, prompt, SQL or file path ever leaves the process.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from redis import Redis
from starlette.concurrency import run_in_threadpool

from setuhaul.booking import make_hold_checker
from setuhaul.clock import Clock, SystemClock
from setuhaul.config import load_settings
from setuhaul.conversation import (
    ConversationServices,
    InboundMessage,
    TrustedConversationContext,
    handle_message,
)
from setuhaul.db import IST, immediate, open_connection, parse_operational_timestamp
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
from setuhaul.identity import AuthError, resolve_driver, verify_channel_request
from setuhaul.jobs.expire_holds import MaintenanceRunner
from setuhaul.memory import ConversationMemoryStore
from setuhaul.model_gateway import (
    ModelGateway,
    ModelMessage,
    ModelPolicy,
    ModelResponse,
    ModelUnavailable,
)
from setuhaul.observability import InMemoryExporter
from setuhaul.rate_limit import RateLimits, check_rate_limit

STALE_PROCESSING_SECONDS = 60
SIGNATURE_WINDOW_SECONDS = 300


class UnconfiguredGateway:
    """Used when no provider is set: every turn takes the safe model-outage path."""

    def complete(self, messages: Sequence[ModelMessage], tools: Sequence[dict[str, Any]], policy: ModelPolicy) -> ModelResponse:
        raise ModelUnavailable("MODEL_NOT_CONFIGURED")


@dataclass
class AppDeps:
    db_path: Path
    redis: Redis
    webhook_secret: bytes
    coordinator_tokens: dict[str, str]  # bearer token -> coordinator name
    fallback_path: Path
    policy: ModelPolicy = field(default_factory=ModelPolicy)
    gateway_factory: Callable[[], ModelGateway] = UnconfiguredGateway
    clock: Clock = field(default_factory=SystemClock)
    rate_limits: RateLimits | None = None
    channel: str = "whatsapp"
    run_maintenance: bool = False
    maintenance_interval_s: float | None = None
    exporter: InMemoryExporter | None = None


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    sender: str = Field(alias="from", min_length=5, max_length=32)
    message_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=2000)


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution_code: str
    note: str = Field(max_length=500)


def _reject(request_id: str, status: int, reason: str, **extra: Any) -> JSONResponse:
    headers = {"Retry-After": str(extra["retry_after_seconds"])} if "retry_after_seconds" in extra else None
    return JSONResponse({"status": "REJECTED", "reason_code": reason, "request_id": request_id, **extra}, status_code=status,
                        headers=headers)


def create_app(deps: AppDeps) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        runner = None
        if deps.run_maintenance:
            runner = MaintenanceRunner(None, deps.clock, deps.maintenance_interval_s,
                                       conn_factory=lambda: open_connection(path=deps.db_path))
            await run_in_threadpool(runner.start)
        try:
            yield
        finally:
            if runner is not None:
                await run_in_threadpool(runner.stop)

    app = FastAPI(title="SetuHaul rescheduling service", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _reject(request.state.request_id, 422, "INVALID_REQUEST")

    @app.exception_handler(Exception)
    async def internal(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"status": "ERROR", "reason_code": "INTERNAL_ERROR", "request_id": getattr(request.state, "request_id", "")},
                            status_code=500)

    # ---- liveness and readiness ------------------------------------------------------------------------
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(request: Request) -> JSONResponse:
        def check() -> str | None:
            try:
                conn = open_connection(path=deps.db_path, readonly=True)
                try:
                    conn.execute("select 1 from schema_migrations limit 1").fetchone()
                finally:
                    conn.close()
            except (sqlite3.Error, FileNotFoundError):
                return "DATABASE_UNAVAILABLE"
            try:
                deps.redis.ping()
            except Exception:  # noqa: BLE001 - any cache failure means not ready
                return "CACHE_UNAVAILABLE"
            return None

        problem = await run_in_threadpool(check)
        if problem:
            return JSONResponse({"ready": False, "reason_code": problem, "request_id": request.state.request_id}, status_code=503)
        return JSONResponse({"ready": True, "request_id": request.state.request_id})

    # ---- driver messages --------------------------------------------------------------------------------
    def process(payload: MessageIn, request_id: str) -> JSONResponse:
        try:
            conn = open_connection(path=deps.db_path)
        except (sqlite3.Error, FileNotFoundError):
            return _reject(request_id, 503, "DATABASE_UNAVAILABLE")
        try:
            return _process(conn, payload, request_id)
        except sqlite3.Error:
            return _reject(request_id, 503, "DATABASE_UNAVAILABLE")
        finally:
            conn.close()

    def _process(conn: sqlite3.Connection, payload: MessageIn, request_id: str) -> JSONResponse:
        clock = deps.clock
        try:
            trusted = resolve_driver(conn, payload.sender)
        except AuthError as exc:
            return _reject(request_id, 403, exc.code)
        driver_id = trusted.driver_id
        fp = hashlib.sha256(payload.text.encode()).hexdigest()
        now_epoch = int(clock.now().timestamp())

        def claim() -> JSONResponse | None:
            """None means we own the message; otherwise the response to return instead."""
            with immediate(conn):
                row = conn.execute("select * from inbound_messages where driver_id=? and message_id=?",
                                   (driver_id, payload.message_id)).fetchone()
                if row is None:
                    return None  # claimed below, after the rate limit
                if row["fingerprint"] != fp:
                    return _reject(request_id, 409, "IDEMPOTENCY_MISMATCH")
                if row["status"] == "DONE":
                    body = json.loads(row["reply_json"])
                    return JSONResponse({**body, "duplicate": True, "request_id": request_id})
                if now_epoch - row["claimed_epoch"] < STALE_PROCESSING_SECONDS:
                    return _reject(request_id, 409, "IN_PROGRESS")
                conn.execute("delete from inbound_messages where driver_id=? and message_id=?", (driver_id, payload.message_id))
                return None

        early = claim()
        if early is not None:
            return early
        limit = check_rate_limit(deps.redis, driver_id, clock, deps.rate_limits, deps.channel)
        if not limit.allowed:
            return _reject(request_id, 429, limit.reason_code or "RATE_LIMITED", retry_after_seconds=limit.retry_after_seconds)
        with immediate(conn):
            cur = conn.execute(
                "insert or ignore into inbound_messages (driver_id, message_id, fingerprint, status, claimed_epoch, created_at) "
                "values (?,?,?, 'PROCESSING',?,?)", (driver_id, payload.message_id, fp, now_epoch, clock.now().isoformat()))
        if cur.rowcount == 0:  # another worker claimed it between our check and our insert
            return _reject(request_id, 409, "IN_PROGRESS")

        try:
            row = conn.execute("select thread_id from chat_threads where driver_id=? and thread_status<>'CLOSED' "
                               "order by opened_at desc, rowid desc limit 1", (driver_id,)).fetchone()
            memory = ConversationMemoryStore(conn, deps.redis, clock, load_settings(), hold_checker=make_hold_checker(conn, clock))
            thread_id = row["thread_id"] if row else memory.open_thread(driver_id)
            services = ConversationServices(
                conn=conn, memory=memory, gateway=deps.gateway_factory(), policy=deps.policy, sink=SqlEscalationSink(conn, clock),
                fallback_sink=JsonlEscalationSink(deps.fallback_path), exporter=deps.exporter)
            reply = handle_message(InboundMessage(message_id=payload.message_id, text=payload.text),
                                   TrustedConversationContext(driver=trusted, thread_id=thread_id), clock, services)
            body = {"status": reply.status, "reply": reply.text, "language": reply.language, "reason_code": reply.escalation_reason,
                    "case_id": reply.case_id}
            with immediate(conn):
                conn.execute("update inbound_messages set status='DONE', reply_json=?, completed_at=? where driver_id=? and message_id=?",
                             (json.dumps(body), clock.now().isoformat(), driver_id, payload.message_id))
            return JSONResponse({**body, "duplicate": False, "request_id": request_id})
        except BaseException:
            with immediate(conn):  # free the id so the provider's retry can succeed
                conn.execute("delete from inbound_messages where driver_id=? and message_id=? and status='PROCESSING'",
                             (driver_id, payload.message_id))
            raise

    @app.post("/v1/driver/messages")
    async def driver_message(request: Request) -> JSONResponse:
        rid = request.state.request_id
        raw = await request.body()
        try:
            verify_channel_request(raw, request.headers.get("x-signature", ""), deps.webhook_secret)
        except AuthError as exc:
            return _reject(rid, 401, exc.code)  # nothing in the body has been parsed yet
        try:
            payload = MessageIn.model_validate(json.loads(raw))
        except (ValueError, ValidationError):
            return _reject(rid, 422, "INVALID_REQUEST")
        return await run_in_threadpool(process, payload, rid)

    # ---- driver status ----------------------------------------------------------------------------------
    def driver_status(phone: str, rid: str) -> JSONResponse:
        try:
            conn = open_connection(path=deps.db_path, readonly=True)
        except (sqlite3.Error, FileNotFoundError):
            return _reject(rid, 503, "DATABASE_UNAVAILABLE")
        try:
            try:
                driver = resolve_driver(conn, phone)
            except AuthError as exc:
                return _reject(rid, 403, exc.code)
            case = get_driver_case_status(conn, driver.driver_id)
            exceptions = [{"exception_id": r["exception_id"], "shipment_id": r["shipment_id"], "type": r["exception_type"],
                           "status": r["exception_status"], "reported_at": r["reported_at"]}
                          for r in conn.execute("select * from driver_exceptions where driver_id=? order by reported_at desc limit 10",
                                                (driver.driver_id,))]
            return JSONResponse({"case": case.model_dump(mode="json") if case else None, "exceptions": exceptions, "request_id": rid})
        except sqlite3.Error:
            return _reject(rid, 503, "DATABASE_UNAVAILABLE")
        finally:
            conn.close()

    @app.get("/v1/driver/status")
    async def driver_status_endpoint(request: Request, phone: str, ts: int) -> JSONResponse:
        rid = request.state.request_id
        if abs(int(deps.clock.now().timestamp()) - ts) > SIGNATURE_WINDOW_SECONDS:
            return _reject(rid, 401, "STALE_REQUEST")
        try:
            verify_channel_request(f"{phone}|{ts}".encode(), request.headers.get("x-signature", ""), deps.webhook_secret)
        except AuthError as exc:
            return _reject(rid, 401, exc.code)
        return await run_in_threadpool(driver_status, phone, rid)

    # ---- coordinator ------------------------------------------------------------------------------------
    def coordinator(request: Request) -> str | None:
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else ""
        for known, name in deps.coordinator_tokens.items():
            if hmac.compare_digest(known, token):
                return name
        return None

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

    def transition_response(rid: str, r: TransitionResult) -> JSONResponse:
        ok = {"CLAIMED", "RESOLVED", "REOPENED"}
        if r.status in ok or (r.status == "ALREADY_CLAIMED" and r.replayed):
            return JSONResponse({"status": "CLAIMED" if r.status == "ALREADY_CLAIMED" else r.status, "case_id": r.case_id, "request_id": rid})
        code = {"NOT_FOUND": 404, "NOT_YOUR_CASE": 403, "INVALID_RESOLUTION": 422}.get(r.status, 409)
        return _reject(rid, code, r.status, case_id=r.case_id, claimed_by=r.claimed_by)

    @app.get("/v1/coordinator/cases")
    async def cases(request: Request, status: str | None = None, limit: int = 50) -> JSONResponse:
        rid = request.state.request_id
        if coordinator(request) is None:
            return _reject(rid, 401, "UNAUTHORIZED")
        statuses = tuple(s for s in (status or "OPEN,CLAIMED").split(",") if s in ("OPEN", "CLAIMED", "RESOLVED", "CANCELLED"))
        return await run_in_threadpool(with_conn, rid, lambda c: JSONResponse(
            {"cases": [x.model_dump(mode="json") for x in list_open_cases(c, statuses or ("OPEN", "CLAIMED"), min(limit, 200))], "request_id": rid}))

    @app.get("/v1/coordinator/cases/{case_id}")
    async def case_detail(request: Request, case_id: str) -> JSONResponse:
        rid = request.state.request_id
        if coordinator(request) is None:
            return _reject(rid, 401, "UNAUTHORIZED")

        def read(c: sqlite3.Connection) -> JSONResponse:
            case = get_case(c, case_id)
            if case is None:
                return _reject(rid, 404, "NOT_FOUND")
            from setuhaul.escalation import case_events

            return JSONResponse({"case": case.model_dump(mode="json"), "events": case_events(c, case_id), "request_id": rid})

        return await run_in_threadpool(with_conn, rid, read)

    @app.post("/v1/coordinator/cases/{case_id}/claim")
    async def claim_endpoint(request: Request, case_id: str) -> JSONResponse:
        rid, who = request.state.request_id, coordinator(request)
        if who is None:
            return _reject(rid, 401, "UNAUTHORIZED")
        return await run_in_threadpool(with_conn, rid, lambda c: transition_response(rid, claim_case(c, case_id, who, deps.clock)))

    @app.post("/v1/coordinator/cases/{case_id}/resolve")
    async def resolve_endpoint(request: Request, case_id: str, body: ResolveIn) -> JSONResponse:
        rid, who = request.state.request_id, coordinator(request)
        if who is None:
            return _reject(rid, 401, "UNAUTHORIZED")
        return await run_in_threadpool(with_conn, rid, lambda c: transition_response(
            rid, resolve_case(c, case_id, who, body.resolution_code, body.note, deps.clock)))

    @app.post("/v1/coordinator/cases/{case_id}/reopen")
    async def reopen_endpoint(request: Request, case_id: str) -> JSONResponse:
        rid, who = request.state.request_id, coordinator(request)
        if who is None:
            return _reject(rid, 401, "UNAUTHORIZED")
        return await run_in_threadpool(with_conn, rid, lambda c: transition_response(rid, reopen_case(c, case_id, who, deps.clock)))

    return app


def create_app_from_env() -> FastAPI:
    """``uvicorn --factory setuhaul.api:create_app_from_env``. Secrets come from the environment only."""
    s = load_settings()
    tokens = json.loads(os.environ.get("SETUHAUL_COORDINATOR_TOKENS", "{}"))
    from setuhaul.model_gateway import build_gateway, load_model_policy

    policy = load_model_policy()
    factory: Callable[[], ModelGateway]
    if os.environ.get("SETUHAUL_USE_STUB_MODEL") == "1":  # load tests and demos only
        from setuhaul.stub_model import StubGateway

        factory = StubGateway
    elif policy.provider:
        gw = build_gateway(policy)
        factory = lambda: gw
    else:
        factory = UnconfiguredGateway
    if s.redis_url.startswith("fakeredis"):  # load tests only: in-process, so run a single worker
        import fakeredis

        redis: Redis = fakeredis.FakeRedis(decode_responses=True)
    else:
        redis = Redis.from_url(s.redis_url, decode_responses=True)
    clock: Clock = SystemClock()
    if os.environ.get("SETUHAUL_FAKE_NOW"):  # load tests against the dated snapshot only
        from setuhaul.clock import FakeClock

        clock = FakeClock(parse_operational_timestamp(os.environ["SETUHAUL_FAKE_NOW"]))
    return create_app(AppDeps(
        db_path=s.db_path, redis=redis,
        webhook_secret=os.environ.get("SETUHAUL_WEBHOOK_SECRET", "").encode(), coordinator_tokens=tokens,
        fallback_path=Path(os.environ.get("SETUHAUL_FALLBACK_ESCALATIONS", "escalations-fallback.jsonl")), policy=policy,
        gateway_factory=factory, run_maintenance=os.environ.get("SETUHAUL_RUN_MAINTENANCE") == "1", clock=clock))


__all__ = ["IST", "AppDeps", "create_app", "create_app_from_env", "parse_operational_timestamp", "timedelta"]
