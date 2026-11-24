"""Admin JSON APIs: invite and list users, disable a user, read the audit log."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from setuhaul.auth import audit, users
from setuhaul.auth.authz import require_role
from setuhaul.auth.http import reject as _reject
from setuhaul.auth.users import Role, User
from setuhaul.db import IST, open_connection
from setuhaul.identity import AuthError

if TYPE_CHECKING:
    from setuhaul.api import AppDeps

# Never return more audit rows than this in one call, whatever the caller asks for.
MAX_AUDIT_LIMIT = 500
# Rows returned when no (or a non-positive) limit is given.
DEFAULT_AUDIT_LIMIT = 200
# Refusal code -> HTTP status; anything unlisted is a 422 (bad input).
_STATUS = {"FORBIDDEN": 403, "UNKNOWN_USER": 404, "ALREADY_INVITED": 409, "CANNOT_DISABLE_SELF": 409, "LAST_ADMIN": 409}


# The body of POST /v1/admin/users; unknown fields are refused.
class NewUser(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1, max_length=254)
    role: Role
    display_name: str = Field(min_length=1, max_length=120)
    facilities: list[str] = Field(default_factory=list, max_length=50)


def check_disable(conn: sqlite3.Connection, *, actor: User, target_id: str) -> None:
    # Nobody disables themselves (they would lock themselves out mid-request).
    if target_id == actor.user_id:
        raise AuthError("CANNOT_DISABLE_SELF")
    target = users.find_by_id(conn, target_id)
    # An unknown id is left for users.disable to refuse (UNKNOWN_USER).
    if target is not None and target.role == "ADMIN" and target.status == "ACTIVE":
        active = conn.execute("select count(*) from users where role='ADMIN' and status='ACTIVE'").fetchone()[0]
        # Keep at least one active admin, or nobody could manage users again.
        if active <= 1:
            raise AuthError("LAST_ADMIN")


def _user_json(user: User, last_login_at: str | None) -> dict[str, Any]:
    # The fields the admin screen shows; no credentials exist on a user.
    return {"user_id": user.user_id, "email": user.email, "role": user.role, "status": user.status,
            "display_name": user.display_name, "facilities": list(user.facilities), "last_login_at": last_login_at}


def _parse_since(raw: str) -> str:
    # ISO timestamp with an offset, else 422; normalised to IST so it sorts like the +05:30 stamps the clock writes.
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise AuthError("INVALID_SINCE") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AuthError("INVALID_SINCE")
    return parsed.astimezone(IST).isoformat()


def register_admin_api(app: FastAPI, deps: AppDeps) -> None:
    # Run a function on a fresh connection; AuthError becomes its status, database trouble 503.
    def with_conn(rid: str, fn: Any, readonly: bool = False) -> Response:
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

    @app.post("/v1/admin/users")
    async def invite_user(request: Request, body: NewUser, user: Annotated[User, Depends(require_role("ADMIN"))]) -> Response:
        rid = request.state.request_id

        def write(conn: sqlite3.Connection) -> Response:
            created = users.invite(conn, actor=user, email=body.email, role=body.role, display_name=body.display_name,
                                   facilities=tuple(body.facilities), clock=deps.clock)
            return JSONResponse({"status": "OK", "user": _user_json(created, None), "request_id": rid}, status_code=201)

        return await run_in_threadpool(with_conn, rid, write)

    @app.get("/v1/admin/users")
    async def list_users(request: Request, user: Annotated[User, Depends(require_role("ADMIN"))]) -> Response:
        rid = request.state.request_id

        def read(conn: sqlite3.Connection) -> Response:
            out = []
            # Every user, oldest first; each one re-read through the users module so the shape is the same everywhere.
            for row in conn.execute("select user_id, last_login_at from users order by created_at, user_id").fetchall():
                found = users.find_by_id(conn, row["user_id"])
                if found is not None:
                    out.append(_user_json(found, row["last_login_at"]))
            return JSONResponse({"users": out, "request_id": rid})

        return await run_in_threadpool(with_conn, rid, read, True)

    @app.post("/v1/admin/users/{user_id}/disable")
    async def disable_user(request: Request, user_id: str, user: Annotated[User, Depends(require_role("ADMIN"))]) -> Response:
        rid = request.state.request_id

        def write(conn: sqlite3.Connection) -> Response:
            try:
                check_disable(conn, actor=user, target_id=user_id)
            except AuthError as exc:
                # A refused attempt is evidence too.
                audit.record(conn, actor_user_id=user.user_id, actor_email=user.email, action="DISABLE", target_type="user",
                             target_id=user_id, outcome="REFUSED", ip=None, detail={"reason": exc.code}, clock=deps.clock)
                conn.commit()
                raise
            users.disable(conn, actor=user, user_id=user_id, clock=deps.clock)
            return JSONResponse({"status": "OK", "user_id": user_id, "request_id": rid})

        return await run_in_threadpool(with_conn, rid, write)

    @app.get("/v1/admin/audit")
    async def audit_log(request: Request, user: Annotated[User, Depends(require_role("ADMIN"))], actor: str | None = None,
                        action: str | None = None, outcome: str | None = None, since: str | None = None,
                        limit: int = DEFAULT_AUDIT_LIMIT) -> Response:
        rid = request.state.request_id

        def read(conn: sqlite3.Connection) -> Response:
            # Validate and normalise before any query runs.
            floor = _parse_since(since) if since is not None else None
            capped = min(limit if limit > 0 else DEFAULT_AUDIT_LIMIT, MAX_AUDIT_LIMIT)
            rows = audit.query(conn, actor=actor, action=action, outcome=outcome, since=floor, limit=capped)
            # Fixed columns only; detail was scrubbed of secrets when written.
            entries = [{"audit_id": r["audit_id"], "ts": r["ts"], "actor_user_id": r["actor_user_id"], "actor_email": r["actor_email"],
                        "action": r["action"], "target_type": r["target_type"], "target_id": r["target_id"], "outcome": r["outcome"],
                        "ip": r["ip"], "detail": json.loads(r["detail_json"]) if r["detail_json"] else None} for r in rows]
            return JSONResponse({"entries": entries, "request_id": rid})

        return await run_in_threadpool(with_conn, rid, read, True)
