"""Warehouse read APIs (partner slot requests, driver chats) and the facility scope of escalation cases."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from setuhaul.auth import projects
from setuhaul.auth.authz import facility_clause, require_role
from setuhaul.auth.http import reject as _reject
from setuhaul.auth.users import User
from setuhaul.db import open_connection

if TYPE_CHECKING:
    from setuhaul.api import AppDeps

# Never return more than this many rows or drivers in one call, whatever the caller asks for.
MAX_LIMIT = 200
# Messages returned per driver, at most.
MAX_MESSAGES = 100


def clamp(value: int, default: int, ceiling: int) -> int:
    # A non-positive limit falls back to the default; anything above the ceiling is cut to it.
    return min(value if value > 0 else default, ceiling)


def case_ids_in_scope(conn: sqlite3.Connection, user: User, statuses: tuple[str, ...] | None = None, limit: int = MAX_LIMIT,
                      case_id: str | None = None) -> list[str]:
    """Ids of the cases whose load is bound for one of the user's facilities (an admin: every case, even one naming no load)."""
    # The facility of a case is the destination facility of its shipment; the scope is applied in the query.
    clause, params = facility_clause(user, "s.destination_facility_id")
    where, args = [clause], list(params)
    if statuses is not None:
        where.append(f"c.status in ({','.join('?' * len(statuses))})")
        args += statuses
    if case_id is not None:
        where.append("c.case_id=?")
        args.append(case_id)
    rows = conn.execute("select c.case_id from escalation_cases c left join shipments s on s.shipment_id=c.shipment_id "
                        f"where {' and '.join(where)} order by c.created_at, c.case_id limit ?", (*args, limit))
    return [r["case_id"] for r in rows]


def register_warehouse_routes(app: FastAPI, deps: AppDeps) -> None:
    # Run a function on a fresh connection; database trouble becomes 503.
    def with_conn(rid: str, fn: Any) -> Response:
        try:
            conn = open_connection(path=deps.db_path, readonly=True)
        except (sqlite3.Error, FileNotFoundError):
            return _reject(rid, 503, "DATABASE_UNAVAILABLE")
        try:
            return fn(conn)  # type: ignore[no-any-return]
        except sqlite3.Error:
            return _reject(rid, 503, "DATABASE_UNAVAILABLE")
        finally:
            conn.close()

    @app.get("/v1/warehouse/partner-requests")
    async def partner_requests(request: Request, user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))],
                               limit: int = 50) -> Response:
        rid = request.state.request_id

        def read(conn: sqlite3.Connection) -> Response:
            # Only the user's facilities (the scope is in the SQL), most recent first.
            clause, params = facility_clause(user, "pr.facility_id")
            rows = conn.execute(
                "select pr.request_ref, pr.shipment_id, pr.facility_id, f.facility_name, pr.proposed_start, pr.proposed_end, pr.status,"
                " pr.email_status, pr.created_at, pr.expires_at, pr.decided_at from partner_requests pr"
                f" join facilities f on f.facility_id=pr.facility_id where {clause} order by pr.created_at desc, pr.request_ref desc limit ?",
                (*params, clamp(limit, 50, MAX_LIMIT)))
            return JSONResponse({"requests": [dict(r) for r in rows], "request_id": rid})

        return await run_in_threadpool(with_conn, rid, read)

    @app.get("/v1/warehouse/driver-chats")
    async def driver_chats(request: Request, user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))],
                           limit: int = 50, messages: int = 20) -> Response:
        rid = request.state.request_id

        def read(conn: sqlite3.Connection) -> Response:
            # A warehouse authority sees only the drivers on their own projects (filtered in SQL); an admin every driver that has a chat.
            if user.role == "ADMIN":
                scope, args = "1=1", tuple[str, ...]()
            else:
                visible = sorted(projects.visible_driver_ids(conn, user))
                # No visible drivers means nothing at all, never "no filter".
                if not visible:
                    return JSONResponse({"drivers": [], "request_id": rid})
                scope, args = f"t.driver_id in ({','.join('?' * len(visible))})", tuple(visible)
            out: list[dict[str, Any]] = []
            per_driver = clamp(messages, 20, MAX_MESSAGES)
            # Most recently active drivers first, capped.
            active = conn.execute(
                "select t.driver_id, max(m.message_ts) as last_ts from chat_messages m join chat_threads t on t.thread_id=m.thread_id"
                f" where m.is_duplicate=0 and {scope} group by t.driver_id order by last_ts desc, t.driver_id limit ?",
                (*args, clamp(limit, 50, MAX_LIMIT))).fetchall()
            for row in active:
                name = conn.execute("select driver_name from drivers where driver_id=?", (row["driver_id"],)).fetchone()
                recent = conn.execute(
                    "select m.sender_type, m.message_text, m.message_ts from chat_messages m join chat_threads t on t.thread_id=m.thread_id"
                    " where t.driver_id=? and m.is_duplicate=0 order by m.message_ts desc, m.chat_message_id desc limit ?",
                    (row["driver_id"], per_driver)).fetchall()
                # Oldest first, so the list reads as a conversation.
                out.append({"driver_id": row["driver_id"], "driver_name": name["driver_name"] if name else None,
                            "messages": [{"role": m["sender_type"], "text": m["message_text"], "timestamp": m["message_ts"]}
                                         for m in reversed(recent)]})
            return JSONResponse({"drivers": out, "request_id": rid})

        return await run_in_threadpool(with_conn, rid, read)
