"""Warehouse routes behind the Shipments tab: availability, assigned shipments and what can be done to one."""

# Lazy annotations.
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from setuhaul.auth import shipments
from setuhaul.auth.authz import require_role
from setuhaul.auth.http import reject
from setuhaul.auth.users import User
from setuhaul.db import open_connection
from setuhaul.identity import AuthError

_CONFLICT = {"DUPLICATE_ORDER_NUMBER", "SLOT_NOT_AVAILABLE", "ALREADY_CONFIRMED", "NOT_LOCKED"}


class DriverChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    driver_id: str
    vehicle_id: str


def _status(code: str) -> int:
    if code.endswith("_NOT_FOUND"):
        return 404
    return 409 if code in _CONFLICT else 422


def register_shipment_routes(app: FastAPI, deps: Any) -> None:
    def run(rid: str, work: Callable[[sqlite3.Connection], Response], readonly: bool = False) -> Response:
        try:
            conn = open_connection(path=deps.db_path, readonly=readonly)
        except (sqlite3.Error, FileNotFoundError):
            return reject(rid, 503, "DATABASE_UNAVAILABLE")
        try:
            return work(conn)
        except AuthError as exc:
            return reject(rid, _status(exc.code), exc.code)
        except sqlite3.Error:
            return reject(rid, 503, "DATABASE_UNAVAILABLE")
        finally:
            conn.close()

    def ok(rid: str, status: int = 200, **fields: Any) -> Response:
        return JSONResponse({"status": "OK", **fields, "request_id": rid}, status_code=status)

    @app.get("/v1/warehouse/shipments")
    async def listed(request: Request, user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id
        return await run_in_threadpool(run, rid, lambda conn: ok(rid, shipments=shipments.list_shipments(conn, user)), True)

    @app.post("/v1/warehouse/availability")
    async def availability(request: Request, body: shipments.Availability,
                           user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id

        def write(conn: sqlite3.Connection) -> Response:
            result = shipments.open_availability(conn, actor=user, data=body, clock=deps.clock)
            return ok(rid, **result.model_dump())

        return await run_in_threadpool(run, rid, write)

    @app.post("/v1/warehouse/shipments")
    async def create(request: Request, body: shipments.NewShipment,
                     user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id

        def write(conn: sqlite3.Connection) -> Response:
            return ok(rid, 201, shipment_id=shipments.create_shipment(conn, actor=user, data=body, clock=deps.clock))

        return await run_in_threadpool(run, rid, write)

    @app.post("/v1/warehouse/shipments/{shipment_id}/driver")
    async def driver(request: Request, shipment_id: str, body: DriverChange,
                     user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id

        def write(conn: sqlite3.Connection) -> Response:
            shipments.change_driver(conn, actor=user, shipment_id=shipment_id, driver_id=body.driver_id,
                                    vehicle_id=body.vehicle_id, clock=deps.clock)
            return ok(rid, shipment_id=shipment_id)

        return await run_in_threadpool(run, rid, write)

    def action(name: str, work: Callable[..., None]) -> None:
        @app.post(f"/v1/warehouse/shipments/{{shipment_id}}/{name}")
        async def handler(request: Request, shipment_id: str,
                          user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
            rid = request.state.request_id

            def write(conn: sqlite3.Connection) -> Response:
                work(conn, actor=user, shipment_id=shipment_id, clock=deps.clock)
                return ok(rid, shipment_id=shipment_id)

            return await run_in_threadpool(run, rid, write)

    action("cancel", shipments.cancel_shipment)
    action("unlock", shipments.unlock)
