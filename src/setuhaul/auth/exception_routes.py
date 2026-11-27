"""Warehouse routes for exceptions: blocking a dock, freeing one booked slot, and deleting or restoring a driver."""

# Lazy annotations.
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from setuhaul import dock_blocks, release
from setuhaul.auth import driver_removal
from setuhaul.auth.authz import require_role
from setuhaul.auth.http import reject
from setuhaul.auth.users import User
from setuhaul.db import open_connection
from setuhaul.identity import AuthError

# The refusal is about what is stored now, not about what was typed.
_CONFLICT = {"DRIVER_ON_SITE", "NOT_DELETED", "PLAN_CHANGED", "NOT_BOOKED", "SLOT_STARTED", "UNLOADING", "TRUCK_ON_SITE"}

# What the person at the screen is told for each refusal.
_MESSAGES = {
    "FACILITY_NOT_FOUND": "That facility is not one of yours.",
    "PARTNER_FACILITY": "Partner warehouses are managed by email, not here.",
    "DOCK_NOT_FOUND": "Choose a dock of that facility.",
    "INVALID_TIMES": "The end must be after the start and after now.",
    "REASON_REQUIRED": "Say why the dock is blocked.",
    "BLOCK_NOT_FOUND": "That block has already ended. Reload the page.",
    "APPOINTMENT_NOT_FOUND": "That booking is not one of yours.",
    "NOT_BOOKED": "That slot is no longer booked. Reload the page.",
    "SLOT_STARTED": "That slot has already started.",
    "UNLOADING": "Unloading has started, so the slot cannot be freed.",
    "TRUCK_ON_SITE": "The truck is already inside, so the slot cannot be freed.",
    "PLAN_CHANGED": "Things have changed since the warning. Check the new list and confirm again.",
    "DRIVER_NOT_FOUND": "That driver is not one you can delete.",
    "OTHER_FACILITY_SHIPMENTS": "This driver has shipments at another warehouse. Ask an administrator to delete them.",
    "DRIVER_ON_SITE": "One of this driver's trucks is at a dock or inside the gate. Finish that visit first.",
    "NAME_MISMATCH": "The name does not match the driver's name.",
    "NOT_DELETED": "That driver is not deleted. Reload the page.",
    "FORBIDDEN": "Only an administrator can restore a driver.",
}


class Confirmed(dock_blocks.BlockRequest):
    # What the warning showed.
    expected: list[dock_blocks.Expected]


class Deletion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm_name: str
    # The shipments the warning listed.
    expected: list[str]


def _status(code: str) -> int:
    if code == "FORBIDDEN":
        return 403
    if code.endswith("_NOT_FOUND"):
        return 404
    return 409 if code in _CONFLICT else 422


def register_exception_routes(app: FastAPI, deps: Any) -> None:
    def run(rid: str, work: Callable[[sqlite3.Connection], Response], readonly: bool = False) -> Response:
        try:
            conn = open_connection(path=deps.db_path, readonly=readonly)
        except (sqlite3.Error, FileNotFoundError):
            return reject(rid, 503, "DATABASE_UNAVAILABLE")
        try:
            return work(conn)
        except AuthError as exc:
            extra = {"items": [i.model_dump() for i in exc.items]} if isinstance(exc, dock_blocks.PlanChanged) else {}
            return JSONResponse({"status": "REJECTED", "reason_code": exc.code, "message": _MESSAGES.get(exc.code),
                                 "request_id": rid, **extra}, status_code=_status(exc.code))
        except sqlite3.Error:
            return reject(rid, 503, "DATABASE_UNAVAILABLE")
        finally:
            conn.close()

    def telegram_on() -> bool:
        from setuhaul.api import telegram_in_force

        return telegram_in_force(deps)[1] is not None

    def ok(rid: str, status: int = 200, **fields: Any) -> Response:
        return JSONResponse({"status": "OK", **fields, "request_id": rid}, status_code=status)

    @app.post("/v1/warehouse/appointments/{appointment_id}/free")
    async def free(request: Request, appointment_id: str,
                   user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id

        def write(conn: sqlite3.Connection) -> Response:
            found = release.free_slot(conn, actor=user, appointment_id=appointment_id, clock=deps.clock)
            reached = release.tell_driver(conn, deps.clock, telegram_on(), found, "slot_freed", order=found.order_reference,
                                          dock=found.dock_code, start=f"{found.start:%H:%M}", date=f"{found.start:%d %b %Y}")
            return ok(rid, appointment_id=appointment_id, order_reference=found.order_reference, reached=reached)

        return await run_in_threadpool(run, rid, write)

    @app.post("/v1/warehouse/dock-blocks/preview")
    async def preview(request: Request, body: dock_blocks.BlockRequest,
                      user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id

        def read(conn: sqlite3.Connection) -> Response:
            items = dock_blocks.preview(conn, actor=user, data=body, clock=deps.clock)
            return ok(rid, items=[i.model_dump() for i in items])

        return await run_in_threadpool(run, rid, read, True)

    @app.post("/v1/warehouse/dock-blocks")
    async def create(request: Request, body: Confirmed,
                     user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id

        def write(conn: sqlite3.Connection) -> Response:
            result = dock_blocks.block(conn, actor=user, data=body, expected=body.expected, clock=deps.clock,
                                       telegram_on=telegram_on())
            return ok(rid, 201, **result.model_dump())

        return await run_in_threadpool(run, rid, write)

    @app.post("/v1/warehouse/dock-blocks/{dock_event_id}/reopen")
    async def reopen(request: Request, dock_event_id: str,
                     user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id

        def write(conn: sqlite3.Connection) -> Response:
            dock_blocks.reopen(conn, actor=user, dock_event_id=dock_event_id, clock=deps.clock)
            return ok(rid, dock_event_id=dock_event_id)

        return await run_in_threadpool(run, rid, write)

    @app.get("/v1/warehouse/drivers/{driver_id}/deletion")
    async def deletion(request: Request, driver_id: str,
                       user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id

        def read(conn: sqlite3.Connection) -> Response:
            return ok(rid, **driver_removal.preview(conn, actor=user, driver_id=driver_id).model_dump())

        return await run_in_threadpool(run, rid, read, True)

    @app.post("/v1/warehouse/drivers/{driver_id}/delete")
    async def delete(request: Request, driver_id: str, body: Deletion,
                     user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id

        def write(conn: sqlite3.Connection) -> Response:
            done = driver_removal.delete(conn, actor=user, driver_id=driver_id, confirm_name=body.confirm_name,
                                         expected=body.expected, clock=deps.clock)
            return ok(rid, **done.model_dump())

        return await run_in_threadpool(run, rid, write)

    @app.post("/v1/drivers/{driver_id}/restore")
    async def restore(request: Request, driver_id: str, user: Annotated[User, Depends(require_role("ADMIN"))]) -> Response:
        rid = request.state.request_id

        def write(conn: sqlite3.Connection) -> Response:
            driver_removal.restore(conn, actor=user, driver_id=driver_id, clock=deps.clock)
            return ok(rid, driver_id=driver_id)

        return await run_in_threadpool(run, rid, write)
