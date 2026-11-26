"""Gate routes (security and admin) and the warehouse's 'unloading complete'. No response ever carries a pass code."""

# Lazy annotations.
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from redis.exceptions import RedisError
from starlette.concurrency import run_in_threadpool

from setuhaul import gate
from setuhaul.auth.authz import require_role
from setuhaul.auth.users import User
from setuhaul.db import open_connection
from setuhaul.identity import AuthError

# What the person at the screen is told, and what to do next.
MESSAGES = {
    "PASS_NOT_VALID": "This pass is not valid. Do not admit the truck: ask the driver to contact their warehouse.",
    "WRONG_WAREHOUSE": "This pass is for another warehouse. Send the driver to the warehouse on their booking.",
    "ALREADY_CHECKED_IN": "This truck has already been checked in. If this is a second truck, do not admit it and tell the warehouse.",
    "TOO_EARLY": "The truck is too early for its slot. Ask the driver to wait until the pass window opens.",
    "TOO_LATE": "The pass window has passed. The warehouse must book again and send a new pass.",
    "NO_APPOINTMENT": "There is no booked slot for this pass. Ask the driver to contact their warehouse.",
    "DRIVER_NOT_ELIGIBLE": "This driver is not approved, active and licensed. Do not admit the truck; tell the warehouse.",
    "DETAILS_DO_NOT_MATCH": "The vehicle number or licence number does not match the pass. Check what you typed against the "
                            "truck and the licence.",
    "TOO_MANY_ATTEMPTS": "Too many failed checks. Wait 15 minutes, or ask an administrator.",
    "NOT_CHECKED_IN": "That truck is not checked in.",
    "UNLOADING_NOT_COMPLETE": "The warehouse has not marked unloading complete. Ask them before the truck leaves.",
    "VISIT_NOT_FOUND": "No such visit at your warehouse.",
}
_CONFLICT = {"ALREADY_CHECKED_IN", "NOT_CHECKED_IN", "UNLOADING_NOT_COMPLETE"}


class CheckOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    truck_visit_id: str | None = None
    code: str | None = Field(None, max_length=80)
    request_key: str = Field(min_length=1, max_length=64)


def _status(code: str) -> int:
    if code.endswith("_NOT_FOUND"):
        return 404
    return {"FORBIDDEN": 403, "TOO_MANY_ATTEMPTS": 429}.get(code, 409 if code in _CONFLICT else 422)


def register_gate_routes(app: FastAPI, deps: Any) -> None:
    def run(rid: str, work: Callable[[sqlite3.Connection], dict[str, Any]], readonly: bool = False) -> Response:
        try:
            conn = open_connection(path=deps.db_path, readonly=readonly)
        except (sqlite3.Error, FileNotFoundError):
            return JSONResponse({"status": "REJECTED", "reason_code": "DATABASE_UNAVAILABLE", "request_id": rid}, status_code=503)
        try:
            return JSONResponse({"status": "OK", **work(conn), "request_id": rid})
        except AuthError as exc:
            return JSONResponse({"status": "REJECTED", "reason_code": exc.code, "message": MESSAGES.get(exc.code), "request_id": rid},
                                status_code=_status(exc.code))
        # Without the failure counter the gate does not decide: refuse rather than allow unlimited guesses.
        except (sqlite3.Error, RedisError):
            return JSONResponse({"status": "REJECTED", "reason_code": "SERVICE_UNAVAILABLE", "request_id": rid,
                                 "message": "The system is not available. Do not admit the truck; contact the warehouse."},
                                status_code=503)
        finally:
            conn.close()

    def checking(path: str, fn: Callable[..., gate.GateView]) -> None:
        @app.post(path)
        async def handler(request: Request, body: gate.GateRequest,
                          user: Annotated[User, Depends(require_role("SECURITY", "ADMIN"))]) -> Response:
            rid = request.state.request_id
            return await run_in_threadpool(
                run, rid, lambda conn: fn(conn, deps.redis, actor=user, data=body, clock=deps.clock).model_dump())

    checking("/v1/gate/validate", gate.validate)
    checking("/v1/gate/check-in", gate.check_in)

    @app.post("/v1/gate/check-out")
    async def check_out(request: Request, body: CheckOut,
                        user: Annotated[User, Depends(require_role("SECURITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id

        def work(conn: sqlite3.Connection) -> dict[str, Any]:
            gate.check_out(conn, actor=user, truck_visit_id=body.truck_visit_id, code=body.code, request_key=body.request_key,
                           clock=deps.clock)
            return {}

        return await run_in_threadpool(run, rid, work)

    @app.get("/v1/gate/visits")
    async def visits(request: Request, user: Annotated[User, Depends(require_role("SECURITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id
        return await run_in_threadpool(run, rid, lambda conn: {"visits": gate.inside(conn, user, deps.clock)}, True)

    @app.post("/v1/warehouse/visits/{truck_visit_id}/unloading-complete")
    async def unloaded(request: Request, truck_visit_id: str,
                       user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id

        def work(conn: sqlite3.Connection) -> dict[str, Any]:
            gate.unloading_complete(conn, actor=user, truck_visit_id=truck_visit_id, clock=deps.clock)
            return {"truck_visit_id": truck_visit_id}

        return await run_in_threadpool(run, rid, work)
