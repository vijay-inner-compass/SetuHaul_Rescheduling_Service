"""Email and password sign-in routes. All of them answer 404 unless the feature is on."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from redis.exceptions import RedisError
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from setuhaul.auth import audit, links, local_admin, sessions, users
from setuhaul.auth.authz import require_role
from setuhaul.auth.http import reject as _reject
from setuhaul.auth.routes import _is_secure
from setuhaul.auth.users import User
from setuhaul.db import open_connection
from setuhaul.identity import AuthError

if TYPE_CHECKING:
    from setuhaul.api import AppDeps

# The longest address worth looking up.
MAX_EMAIL = 254
# The one answer "Forgot password?" gives, whether or not the account exists.
FORGOT_REPLY = "If that address has access, we have sent it a link."

_log = logging.getLogger(__name__)


def send_reset(deps: AppDeps, email: str) -> None:
    """After the reply: send a reset link if the address belongs to an active user."""
    try:
        conn = open_connection(path=deps.db_path)
        try:
            user = users.find_by_email(conn, email)
        finally:
            conn.close()
        if user is not None and user.status == "ACTIVE":
            links.send(deps, user.user_id, "RESET", forgot=True)
    except (sqlite3.Error, FileNotFoundError) as exc:
        _log.warning("password reset not sent: %s", type(exc).__name__)


async def read_strings(request: Request, *names: str) -> tuple[str, ...] | None:
    """The named string fields of a JSON object body, or None for anything else. Used by routes anyone can call."""
    # Another site's form can send text that parses as JSON, and so sign a visitor in as someone else; it cannot set this
    # content type, and a script that does is stopped by the browser's preflight.
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        return None
    try:
        body = await request.json()
        values = tuple(body[name] for name in names)
        for value in values:
            if not isinstance(value, str):
                return None
            # A lone surrogate is legal JSON but cannot be hashed or stored.
            value.encode("utf-8")
    except (ValueError, KeyError, TypeError, RecursionError):
        return None
    return values


def with_conn(deps: AppDeps, rid: str, fn: Callable[[sqlite3.Connection], Response]) -> Response:
    # A fresh connection per call; database trouble becomes 503.
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


def register_password_routes(app: FastAPI, deps: AppDeps) -> None:
    @app.post("/auth/password")
    async def password_login(request: Request) -> Response:
        rid = request.state.request_id
        if not deps.password_login_on:
            return _reject(rid, 404, "NOT_FOUND")
        ip = request.client.host if request.client else None
        fields = await read_strings(request, "email", "password")
        # Refused before any hashing work, and with nothing that says why.
        if fields is None or len(fields[0]) > MAX_EMAIL or len(fields[1]) > local_admin.MAX_LENGTH:
            def refused(conn: sqlite3.Connection) -> Response:
                # Audited like every other attempt, but with none of the submitted values.
                audit.record(conn, actor_user_id=None, actor_email=None, action="PASSWORD_LOGIN", target_type=None,
                             target_id=None, outcome="PASSWORD_REFUSED", ip=ip, detail={"reason": "BAD_REQUEST"},
                             clock=deps.clock)
                conn.commit()
                return _reject(rid, 401, "BAD_CREDENTIALS")

            return await run_in_threadpool(with_conn, deps, rid, refused)
        email, password = fields

        def attempt(conn: sqlite3.Connection) -> Response:
            # verify audits every outcome itself; scrypt makes this blocking, so it runs in the threadpool.
            try:
                user = local_admin.verify(conn, email, password, deps.clock, ip, admin_only=False)
            except AuthError:
                # Locked, unknown, disabled and password-less all look like a wrong password.
                return _reject(rid, 401, "BAD_CREDENTIALS")
            token = sessions.create(conn, user, "PASSWORD", deps.clock)
            conn.commit()
            resp = JSONResponse({"status": "OK", "email": user.email, "request_id": rid})
            resp.set_cookie(sessions.COOKIE_NAME, token, max_age=8 * 3600,
                            **sessions.cookie_kwargs(_is_secure(request, deps.public_url)))  # type: ignore[arg-type]
            return resp

        return await run_in_threadpool(with_conn, deps, rid, attempt)

    @app.post("/auth/password/set")
    async def set_password(request: Request) -> Response:
        rid = request.state.request_id
        if not deps.password_login_on:
            return _reject(rid, 404, "NOT_FOUND")
        ip = request.client.host if request.client else None
        fields = await read_strings(request, "token", "password")
        if fields is None:
            return _reject(rid, 422, "INVALID_REQUEST")
        token, password = fields

        def redeem(conn: sqlite3.Connection) -> Response:
            try:
                links.redeem(conn, token, password, deps.clock, ip)
            except AuthError as exc:
                return _reject(rid, 422, exc.code)
            # No session: the user signs in with the new password, and whoever is signed in here stays as they were.
            return JSONResponse({"status": "OK", "request_id": rid})

        return await run_in_threadpool(with_conn, deps, rid, redeem)

    @app.post("/auth/password/forgot")
    async def forgot_password(request: Request) -> Response:
        rid = request.state.request_id
        if not deps.password_login_on:
            return _reject(rid, 404, "NOT_FOUND")
        ip = request.client.host if request.client else None
        fields = await read_strings(request, "email")
        if fields is None:
            return _reject(rid, 422, "INVALID_REQUEST")
        email = fields[0].strip()
        # Checked before anything is looked up, so the answer is the same whoever was asked about.
        try:
            await run_in_threadpool(deps.redis.ping)
        except RedisError:
            return _reject(rid, 503, "TRY_LATER")

        def requested(conn: sqlite3.Connection) -> Response:
            audit.record(conn, actor_user_id=None, actor_email=email[:MAX_EMAIL], action="ACCOUNT_LINK", target_type=None,
                         target_id=None, outcome="REQUESTED", ip=ip, detail=None, clock=deps.clock)
            conn.commit()
            return JSONResponse({"status": "OK", "message": FORGOT_REPLY, "request_id": rid})

        resp = await run_in_threadpool(with_conn, deps, rid, requested)
        if resp.status_code == 200 and len(email) <= MAX_EMAIL:
            # Looked up and sent after the reply, so its timing does not reveal whether the account exists.
            resp.background = BackgroundTask(send_reset, deps, email)
        return resp

    @app.post("/v1/admin/users/{user_id}/password-link")
    async def password_link(request: Request, user_id: str,
                            user: Annotated[User, Depends(require_role("ADMIN"))]) -> Response:
        rid = request.state.request_id
        if not deps.password_login_on:
            return _reject(rid, 404, "NOT_FOUND")

        def send(conn: sqlite3.Connection) -> Response:
            target = users.find_by_id(conn, user_id)
            if target is None:
                return _reject(rid, 404, "UNKNOWN_USER")
            if target.status != "ACTIVE":
                return _reject(rid, 409, "USER_DISABLED")
            has = conn.execute("select 1 from admin_credentials where user_id=?", (user_id,)).fetchone()
            sent = links.send(deps, user_id, "RESET" if has else "INVITE")
            audit.record(conn, actor_user_id=user.user_id, actor_email=user.email, action="SEND_PASSWORD_LINK",
                         target_type="user", target_id=user_id, outcome=sent, ip=None, detail=None, clock=deps.clock)
            conn.commit()
            if sent == "THROTTLED":
                return _reject(rid, 429, "TOO_SOON")
            if sent == "FAILED":
                return _reject(rid, 502, "EMAIL_FAILED")
            return JSONResponse({"status": "OK", "user_id": user_id, "request_id": rid})

        return await run_in_threadpool(with_conn, deps, rid, send)
