"""Warehouse read APIs (partner slot requests, driver chats) and the facility scope of escalation cases."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from setuhaul.auth import projects, telegram
from setuhaul.auth.authz import AuthzError, assert_facility, facility_clause, require_role
from setuhaul.auth.http import reject as _reject
from setuhaul.auth.onboarding import NOT_DELETED
from setuhaul.auth.users import User
from setuhaul.board import build_board
from setuhaul.db import TimestampError, open_connection, parse_operational_timestamp
from setuhaul.identity import AuthError

if TYPE_CHECKING:
    from setuhaul.api import AppDeps

# Never return more than this many rows or drivers in one call, whatever the caller asks for.
MAX_LIMIT = 200
# Messages returned per driver, at most.
MAX_MESSAGES = 100


# Refusal code -> HTTP status for the writes below.
_STATUS = {"FORBIDDEN": 403, "DRIVER_NOT_FOUND": 404, "NOT_BOUND": 404, "NOT_REVOKED": 409}


def clamp(value: int, default: int, ceiling: int) -> int:
    # A non-positive limit falls back to the default; anything above the ceiling is cut to it.
    return min(value if value > 0 else default, ceiling)


def case_ids_in_scope(conn: sqlite3.Connection, user: User, statuses: tuple[str, ...] | None = None, limit: int = MAX_LIMIT,
                      case_id: str | None = None, resolved_since: str | None = None) -> list[str]:
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
    if resolved_since is not None:
        # julianday compares moments, whatever offset each timestamp was written with.
        where.append("julianday(c.resolved_at) >= julianday(?)")
        args.append(resolved_since)
    rows = conn.execute("select c.case_id from escalation_cases c left join shipments s on s.shipment_id=c.shipment_id "
                        f"where {' and '.join(where)} order by c.created_at, c.case_id limit ?", (*args, limit))
    return [r["case_id"] for r in rows]


def mask_phone(phone: str) -> str:
    # Only the last four digits survive; the country code stays so the number still reads as a phone.
    digits = "".join(c for c in phone if c.isdigit())
    country = digits[:-10] if len(digits) > 10 else ""
    return (f"+{country}" if country else "") + "\u2022" * 6 + digits[-4:]


def drivers_in_scope(conn: sqlite3.Connection, user: User, today: date, driver_id: str | None = None) -> list[sqlite3.Row]:
    """Drivers the user may see: an admin every driver; a warehouse authority those they added plus their live project drivers."""
    where, args = ["1=1"], []
    if user.role != "ADMIN":
        visible = sorted(projects.visible_driver_ids(conn, user, today))
        # No visible project drivers leaves only the drivers they added, never "no filter".
        marks = f" or d.driver_id in ({','.join('?' * len(visible))})" if visible else ""
        where = [f"(p.created_by_user_id=?{marks})"]
        args = [user.user_id, *visible]
    where.append(NOT_DELETED)
    if driver_id is not None:
        where.append("d.driver_id=?")
        args.append(driver_id)
    return conn.execute(
        "select d.driver_id, d.driver_name, d.phone, d.carrier_id, p.vehicle_id, p.approval_status, p.licence_expiry,"
        " t.driver_id as bound_id, t.revoked_at from drivers d left join driver_profiles p on p.driver_id=d.driver_id"
        f" left join telegram_identities t on t.driver_id=d.driver_id where {' and '.join(where)} order by d.driver_id",
        args).fetchall()


def partner_requests_in_scope(conn: sqlite3.Connection, user: User, limit: int = 50) -> list[dict[str, Any]]:
    """Partner slot requests for the user's facilities (an admin: all), most recent first."""
    # Only the user's facilities (the scope is in the SQL), most recent first.
    clause, params = facility_clause(user, "pr.facility_id")
    rows = conn.execute(
        "select pr.request_ref, pr.shipment_id, pr.facility_id, f.facility_name, pr.proposed_start, pr.proposed_end, pr.status,"
        " pr.email_status, pr.created_at, pr.expires_at, pr.decided_at from partner_requests pr"
        f" join facilities f on f.facility_id=pr.facility_id where {clause} order by pr.created_at desc, pr.request_ref desc limit ?",
        (*params, clamp(limit, 50, MAX_LIMIT)))
    return [dict(r) for r in rows]


def _facility_chats(conn: sqlite3.Connection, user: User, limit: int, per_driver: int) -> list[dict[str, Any]]:
    # A warehouse authority: only messages recorded for a load going to one of their facilities. The scope is in the SQL,
    # and no facility means nothing at all. Bounded in SQL so cost does not grow with total message history (F08).
    clause, params = facility_clause(user, "l.facility_id")
    active = conn.execute(
        "select t.driver_id, max(m.message_ts) as last_ts from chat_message_loads l"
        " join chat_messages m using(chat_message_id) join chat_threads t on t.thread_id=m.thread_id"
        f" where m.is_duplicate=0 and {clause} group by t.driver_id order by last_ts desc, t.driver_id limit ?",
        (*params, limit),
    ).fetchall()
    ranked: list[tuple[float, str, list[tuple[datetime, int, sqlite3.Row]]]] = []
    for row in active:
        driver_id = str(row["driver_id"])
        msgs = conn.execute(
            "select m.rowid as seq, m.sender_type, m.message_text, m.message_ts from chat_message_loads l"
            " join chat_messages m using(chat_message_id) join chat_threads t on t.thread_id=m.thread_id"
            f" where m.is_duplicate=0 and t.driver_id=? and {clause} order by m.message_ts desc, m.rowid desc limit ?",
            (driver_id, *params, per_driver),
        ).fetchall()
        parsed: list[tuple[datetime, int, sqlite3.Row]] = []
        for m in msgs:
            try:
                ts = parse_operational_timestamp(m["message_ts"])
            except TimestampError:
                continue
            parsed.append((ts, int(m["seq"]), m))
        if parsed:
            ranked.append((-max(p[0] for p in parsed).timestamp(), driver_id, parsed))
    ranked.sort(key=lambda item: (item[0], item[1]))
    out: list[dict[str, Any]] = []
    for _, driver_id, parsed in ranked:
        recent = sorted(parsed, key=lambda k: (k[0], k[1]))[-per_driver:]
        name = conn.execute("select driver_name from drivers where driver_id=?", (driver_id,)).fetchone()
        out.append({"driver_id": driver_id, "driver_name": name["driver_name"] if name else None,
                    "messages": [{"role": m["sender_type"], "text": m["message_text"], "timestamp": m["message_ts"]}
                                 for _, _, m in recent]})
    return out


def driver_chats_in_scope(conn: sqlite3.Connection, user: User, now: datetime, limit: int = 50,
                          messages: int = 20) -> list[dict[str, Any]]:
    """Recent messages of the drivers the user may see.

    A warehouse authority: only messages about loads going to their facilities, whoever added the driver.
    An admin: every chatting driver, whole history.
    """
    per_driver = clamp(messages, 20, MAX_MESSAGES)
    if user.role != "ADMIN":
        return _facility_chats(conn, user, clamp(limit, 50, MAX_LIMIT), per_driver)
    out: list[dict[str, Any]] = []
    # Most recently active drivers first, capped.
    active = conn.execute(
        "select t.driver_id, max(m.message_ts) as last_ts from chat_messages m join chat_threads t on t.thread_id=m.thread_id"
        " where m.is_duplicate=0 group by t.driver_id order by last_ts desc, t.driver_id limit ?",
        (clamp(limit, 50, MAX_LIMIT),)).fetchall()
    for row in active:
        name = conn.execute("select driver_name from drivers where driver_id=?", (row["driver_id"],)).fetchone()
        recent = conn.execute(
            "select m.sender_type, m.message_text, m.message_ts from chat_messages m join chat_threads t on t.thread_id=m.thread_id"
            " where t.driver_id=? and m.is_duplicate=0 order by m.message_ts desc, m.rowid desc limit ?",
            (row["driver_id"], per_driver)).fetchall()
        # Oldest first, so the list reads as a conversation.
        out.append({"driver_id": row["driver_id"], "driver_name": name["driver_name"] if name else None,
                    "messages": [{"role": m["sender_type"], "text": m["message_text"], "timestamp": m["message_ts"]}
                                 for m in reversed(recent)]})
    return out


def driver_list(conn: sqlite3.Connection, user: User, today: date) -> list[dict[str, Any]]:
    """The drivers the user may see, phone masked and licence number never included."""
    # Same rule as onboarding.licence_valid, from the columns already fetched (no profile means supplied, so valid).
    out = []
    for r in drivers_in_scope(conn, user, today):
        # Never-bound, bound, or cut off.
        tg = "UNBOUND" if r["bound_id"] is None else ("REVOKED" if r["revoked_at"] else "BOUND")
        # Licence numbers are never returned; the phone only in masked form.
        out.append({"driver_id": r["driver_id"], "driver_name": r["driver_name"], "phone": mask_phone(r["phone"]),
                    "carrier_id": r["carrier_id"], "vehicle_id": r["vehicle_id"],
                    "approval_status": r["approval_status"] or "SUPPLIED", "licence_expiry": r["licence_expiry"],
                    "licence_valid": r["approval_status"] is None or (
                        r["approval_status"] == "APPROVED" and date.fromisoformat(r["licence_expiry"]) >= today), "telegram_status": tg})
    return out


def register_warehouse_routes(app: FastAPI, deps: AppDeps) -> None:
    # Run a function on a fresh connection; database trouble becomes 503.
    def with_conn(rid: str, fn: Any, readonly: bool = True) -> Response:
        try:
            conn = open_connection(path=deps.db_path, readonly=readonly)
        except (sqlite3.Error, FileNotFoundError):
            return _reject(rid, 503, "DATABASE_UNAVAILABLE")
        try:
            return fn(conn)  # type: ignore[no-any-return]
        except AuthError as exc:
            return _reject(rid, _STATUS.get(exc.code, 422), exc.code)
        except sqlite3.Error:
            return _reject(rid, 503, "DATABASE_UNAVAILABLE")
        finally:
            conn.close()

    @app.get("/v1/warehouse/partner-requests")
    async def partner_requests(request: Request, user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))],
                               limit: int = 50) -> Response:
        rid = request.state.request_id

        def read(conn: sqlite3.Connection) -> Response:
            return JSONResponse({"requests": partner_requests_in_scope(conn, user, limit), "request_id": rid})

        return await run_in_threadpool(with_conn, rid, read)

    @app.get("/v1/warehouse/driver-chats")
    async def driver_chats(request: Request, user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))],
                           limit: int = 50, messages: int = 20) -> Response:
        rid = request.state.request_id

        def read(conn: sqlite3.Connection) -> Response:
            return JSONResponse({"drivers": driver_chats_in_scope(conn, user, deps.clock.now(), limit, messages), "request_id": rid})

        return await run_in_threadpool(with_conn, rid, read)

    @app.get("/v1/warehouse/board")
    async def board(request: Request, user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))],
                    facility_id: str) -> Response:
        rid = request.state.request_id

        def read(conn: sqlite3.Connection) -> Response:
            # A facility outside the user's scope looks exactly like one that does not exist.
            try:
                assert_facility(user, facility_id)
            except AuthzError:
                return _reject(rid, 404, "FACILITY_NOT_FOUND")
            built = build_board(conn, facility_id, deps.clock.now())
            if built is None:
                return _reject(rid, 404, "FACILITY_NOT_FOUND")
            return JSONResponse({**built, "request_id": rid})

        return await run_in_threadpool(with_conn, rid, read)

    @app.get("/v1/warehouse/open-slots")
    async def warehouse_open_slots(
        request: Request,
        user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))],
        facility_id: str,
        day: str | None = None,
        limit: int = 200,
    ) -> Response:
        from datetime import date as _date

        from setuhaul.assign import open_slots_page

        rid = request.state.request_id
        parsed_day: _date | None = None
        if day:
            try:
                parsed_day = _date.fromisoformat(day)
            except ValueError:
                return _reject(rid, 422, "INVALID_REQUEST")

        def read(conn: sqlite3.Connection) -> Response:
            try:
                assert_facility(user, facility_id)
            except AuthzError:
                return _reject(rid, 404, "FACILITY_NOT_FOUND")
            rows, has_more = open_slots_page(
                conn, facility_id, deps.clock.now(), day=parsed_day, limit=clamp(limit, 200, 500)
            )
            items = [
                {
                    "slot_id": r["slot_id"],
                    "label": f"{facility_id} {r['dock_code']} {parse_operational_timestamp(r['slot_start_ts']):%d %b %Y %H:%M}",
                }
                for r in rows
            ]
            return JSONResponse({"slots": items, "has_more": has_more, "request_id": rid})

        return await run_in_threadpool(with_conn, rid, read)

    @app.get("/v1/warehouse/drivers")
    async def drivers(request: Request, user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id

        def read(conn: sqlite3.Connection) -> Response:
            return JSONResponse({"drivers": driver_list(conn, user, deps.clock.now().date()), "request_id": rid})

        return await run_in_threadpool(with_conn, rid, read)

    @app.post("/v1/warehouse/drivers/{driver_id}/telegram/revoke")
    async def revoke_telegram(request: Request, driver_id: str,
                              user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id

        def write(conn: sqlite3.Connection) -> Response:
            # Admin, or the authority who onboarded the driver; anyone else gets DRIVER_NOT_FOUND (checked in telegram.revoke).
            telegram.revoke(conn, actor=user, driver_id=driver_id, clock=deps.clock)
            return JSONResponse({"status": "OK", "driver_id": driver_id, "telegram_status": "REVOKED", "request_id": rid})

        return await run_in_threadpool(with_conn, rid, write, False)

    @app.post("/v1/warehouse/drivers/{driver_id}/telegram/allow-relink")
    async def allow_relink(request: Request, driver_id: str,
                           user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id

        def write(conn: sqlite3.Connection) -> Response:
            # Same scope rule as revoke; clears the revoked row so the driver may share their contact again.
            telegram.allow_relink(conn, actor=user, driver_id=driver_id, clock=deps.clock)
            return JSONResponse({"status": "OK", "driver_id": driver_id, "telegram_status": "UNBOUND", "request_id": rid})

        return await run_in_threadpool(with_conn, rid, write, False)
