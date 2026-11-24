"""Driver onboarding routes: add (warehouse or admin), list pending, approve and reject (admin)."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import sqlite3
from datetime import date
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from setuhaul.auth import onboarding
from setuhaul.auth.authz import require_role
from setuhaul.auth.http import reject as _reject
from setuhaul.auth.users import User
from setuhaul.db import open_connection
from setuhaul.identity import AuthError

if TYPE_CHECKING:
    from setuhaul.api import AppDeps

# Refusal code -> HTTP status; anything unlisted is a 422 (bad input).
_STATUS = {"FORBIDDEN": 403, "DRIVER_NOT_FOUND": 404, "DUPLICATE_PHONE": 409, "DUPLICATE_LICENCE": 409,
           "NOT_PENDING": 409, "LICENCE_EXPIRED": 409}


# The body of POST /v1/drivers; unknown fields are refused.
class NewDriver(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=1, max_length=32)
    licence_number: str = Field(min_length=1, max_length=40)
    licence_expiry: date
    carrier_id: str = Field(min_length=1, max_length=32)
    vehicle_id: str = Field(min_length=1, max_length=32)


def register_admin_routes(app: FastAPI, deps: AppDeps) -> None:
    # Run a function on a fresh connection; AuthError becomes its status, database trouble 503.
    def with_conn(rid: str, fn: Any) -> Response:
        try:
            conn = open_connection(path=deps.db_path)
        except (sqlite3.Error, FileNotFoundError):
            return _reject(rid, 503, "DATABASE_UNAVAILABLE")
        try:
            return fn(conn)  # type: ignore[no-any-return]
        except AuthError as exc:
            return _reject(rid, _STATUS.get(exc.code, 422), exc.code)
        except sqlite3.IntegrityError as exc:
            # The UNIQUE constraints are the backstop for a race the up-front checks lost.
            return _reject(rid, 409, "DUPLICATE_PHONE" if "phone" in str(exc) else "DUPLICATE_LICENCE")
        except sqlite3.Error:
            return _reject(rid, 503, "DATABASE_UNAVAILABLE")
        finally:
            conn.close()

    @app.post("/v1/drivers")
    async def add_driver(request: Request, body: NewDriver,
                         user: Annotated[User, Depends(require_role("ADMIN", "WAREHOUSE_AUTHORITY"))]) -> Response:
        rid = request.state.request_id

        def write(conn: sqlite3.Connection) -> Response:
            driver_id = onboarding.add_driver(
                conn, actor=user, name=body.name, phone=body.phone, licence_number=body.licence_number,
                licence_expiry=body.licence_expiry, carrier_id=body.carrier_id, vehicle_id=body.vehicle_id,
                clock=deps.clock)
            return JSONResponse({"status": "OK", "driver_id": driver_id, "approval_status": "PENDING_APPROVAL",
                                 "request_id": rid}, status_code=201)

        return await run_in_threadpool(with_conn, rid, write)

    @app.get("/v1/drivers/pending")
    async def pending(request: Request, user: Annotated[User, Depends(require_role("ADMIN"))]) -> Response:
        rid = request.state.request_id

        def read(conn: sqlite3.Connection) -> Response:
            rows = [dict(r) for r in onboarding.pending(conn)]
            return JSONResponse({"drivers": rows, "request_id": rid})

        return await run_in_threadpool(with_conn, rid, read)

    def decision(approve: bool) -> Any:
        async def handler(request: Request, driver_id: str,
                          user: Annotated[User, Depends(require_role("ADMIN"))]) -> Response:
            rid = request.state.request_id

            def write(conn: sqlite3.Connection) -> Response:
                fn = onboarding.approve if approve else onboarding.reject
                fn(conn, actor=user, driver_id=driver_id, clock=deps.clock)
                return JSONResponse({"status": "OK", "driver_id": driver_id,
                                     "approval_status": "APPROVED" if approve else "REJECTED", "request_id": rid})

            return await run_in_threadpool(with_conn, rid, write)

        return handler

    app.post("/v1/drivers/{driver_id}/approve")(decision(True))
    app.post("/v1/drivers/{driver_id}/reject")(decision(False))
