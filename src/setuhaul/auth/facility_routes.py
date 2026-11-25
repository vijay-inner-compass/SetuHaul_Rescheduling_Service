"""Warehouse routes for facility change requests: list those headed to my facilities, approve or reject."""

# Lazy annotations.
from __future__ import annotations

import sqlite3
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from setuhaul import facility_change
from setuhaul.auth.authz import facility_clause, require_role
from setuhaul.auth.http import reject
from setuhaul.auth.users import User
from setuhaul.db import open_connection


def pending_changes_in_scope(conn: sqlite3.Connection, user: User) -> list[dict[str, Any]]:
    """Open requests whose DESTINATION is one of the user's facilities (admins: all), oldest first."""
    clause, params = facility_clause(user, "q.to_facility_id")
    rows = conn.execute(
        "select q.request_id, q.shipment_id, q.created_at, q.expires_at, d.driver_name, s.order_reference, f.facility_name as origin, "
        "t.facility_name as destination from facility_change_requests q join drivers d using(driver_id) join shipments s using(shipment_id) "
        "join facilities f on f.facility_id=q.from_facility_id join facilities t on t.facility_id=q.to_facility_id "
        f"where q.status='PENDING' and {clause} order by q.created_at", params).fetchall()
    return [dict(r) for r in rows]


def register_facility_routes(app: FastAPI, deps: Any) -> None:
    @app.get("/v1/warehouse/facility-changes")
    async def changes(request: Request, user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Any:
        def read() -> Any:
            conn = open_connection(path=deps.db_path, readonly=True)
            try:
                return {"requests": pending_changes_in_scope(conn, user)}
            finally:
                conn.close()

        return await run_in_threadpool(read)

    def decide(request_id: str, user: User, approve: bool, rid: str) -> Any:
        conn = open_connection(path=deps.db_path)
        try:
            result = facility_change.decide(conn, deps.clock, actor=user, request_id=request_id, approve=approve)
            if result.status == "NOT_FOUND":
                return reject(rid, 404, "NOT_FOUND")
            if result.status not in ("APPROVED", "REJECTED"):
                return reject(rid, 409, result.status)
            if not approve:
                facility_change.tell_rejected(conn, deps.redis, deps.clock, request_id)
                return JSONResponse({"status": "REJECTED", "request_id": request_id})
            outcome = facility_change.after_approval(conn, deps.redis, deps.clock, request_id)
            return JSONResponse({"status": "APPROVED", "request_id": request_id, "outcome": outcome})
        finally:
            conn.close()

    @app.post("/v1/warehouse/facility-changes/{request_id}/approve")
    async def approve(request: Request, request_id: str, user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Any:
        return await run_in_threadpool(decide, request_id, user, True, request.state.request_id)

    @app.post("/v1/warehouse/facility-changes/{request_id}/reject")
    async def refuse(request: Request, request_id: str, user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Any:
        return await run_in_threadpool(decide, request_id, user, False, request.state.request_id)
