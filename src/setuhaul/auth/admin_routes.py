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

from setuhaul.auth import onboarding, projects
from setuhaul.auth.authz import require_role
from setuhaul.auth.http import reject as _reject
from setuhaul.auth.users import User
from setuhaul.db import open_connection
from setuhaul.identity import AuthError
from setuhaul.langsmith_report import load_summary

if TYPE_CHECKING:
    from setuhaul.api import AppDeps

# Refusal code -> HTTP status; anything unlisted is a 422 (bad input).
_STATUS = {"FORBIDDEN": 403, "DRIVER_NOT_FOUND": 404, "DUPLICATE_PHONE": 409, "DUPLICATE_LICENCE": 409,
           "NOT_PENDING": 409, "DRIVER_DELETED": 409, "DRIVER_DELETED_RESTORE": 409, "LICENCE_EXPIRED": 409, "PROJECT_NOT_FOUND": 404, "FACILITY_NOT_FOUND": 404,
           "NOT_ALLOCATED": 404, "DUPLICATE_CONTRACT": 409, "ALREADY_ALLOCATED": 409, "PROJECT_CLOSED": 409,
           "PROJECT_ENDED": 409}


# The body of POST /v1/drivers; unknown fields are refused.
class NewDriver(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=1, max_length=32)
    licence_number: str = Field(min_length=1, max_length=40)
    licence_expiry: date
    carrier_id: str = Field(min_length=1, max_length=32)
    vehicle_id: str = Field(min_length=1, max_length=32)


# The body of POST /v1/projects; unknown fields are refused.
class NewProject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_name: str = Field(min_length=1, max_length=120)
    # The exact pattern is applied by the library after trimming; this only bounds the raw input.
    contract_ref: str = Field(min_length=1, max_length=64)
    facility_id: str = Field(min_length=1, max_length=32)
    starts_on: date
    ends_on: date | None = None


# The body of the allocate and release routes.
class DriverRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    driver_id: str = Field(min_length=1, max_length=32)


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
            # The UNIQUE constraints are the backstop for a race the up-front checks lost; the column name says which.
            message = str(exc)
            if "UNIQUE constraint failed" in message:
                for needle, code in (("phone", "DUPLICATE_PHONE"), ("licence", "DUPLICATE_LICENCE"),
                                     ("contract", "DUPLICATE_CONTRACT")):
                    if needle in message:
                        return _reject(rid, 409, code)
            # Any other integrity failure (foreign key, not null, check) is a database fault, not a duplicate.
            return _reject(rid, 503, "DATABASE_UNAVAILABLE")
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

    # The LangSmith summary for the Model tab, read only when the admin presses the button.
    @app.get("/v1/admin/model/langsmith")
    async def langsmith_summary(user: Annotated[User, Depends(require_role("ADMIN"))]) -> Response:
        from setuhaul.api import env_in_force

        def read() -> dict[str, Any]:
            if deps.settings is None:
                return load_summary()
            conn = open_connection(path=deps.db_path, readonly=True)
            try:
                settings = env_in_force(deps, conn)
            finally:
                conn.close()
            # Read outside the connection: LangSmith can be slow.
            return load_summary(settings)

        return JSONResponse(await run_in_threadpool(read))

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

    # Projects: both roles (the guard is spelled inline so FastAPI can resolve it); the library applies the facility scope and answers 404 for projects out of scope.
    @app.post("/v1/projects")
    async def create_project(request: Request, body: NewProject,
                             user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id

        def write(conn: sqlite3.Connection) -> Response:
            pid = projects.create(conn, actor=user, customer_name=body.customer_name, contract_ref=body.contract_ref,
                                  facility_id=body.facility_id, starts_on=body.starts_on, ends_on=body.ends_on,
                                  clock=deps.clock)
            return JSONResponse({"status": "OK", "project_id": pid, "request_id": rid}, status_code=201)

        return await run_in_threadpool(with_conn, rid, write)

    @app.get("/v1/projects")
    async def list_projects(request: Request, user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id

        def read(conn: sqlite3.Connection) -> Response:
            return JSONResponse({"projects": projects.list_projects(conn, user), "request_id": rid})

        return await run_in_threadpool(with_conn, rid, read)

    def allocation(allocate: bool) -> Any:
        async def handler(request: Request, project_id: str, body: DriverRef,
                          user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
            rid = request.state.request_id

            def write(conn: sqlite3.Connection) -> Response:
                fn = projects.allocate if allocate else projects.release
                fn(conn, actor=user, project_id=project_id, driver_id=body.driver_id, clock=deps.clock)
                return JSONResponse({"status": "OK", "project_id": project_id, "driver_id": body.driver_id,
                                     "request_id": rid})

            return await run_in_threadpool(with_conn, rid, write)

        return handler

    app.post("/v1/projects/{project_id}/allocate")(allocation(True))
    app.post("/v1/projects/{project_id}/release")(allocation(False))
