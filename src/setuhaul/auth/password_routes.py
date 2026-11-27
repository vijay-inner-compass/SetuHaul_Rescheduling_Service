"""Email and password sign-in routes. All of them answer 404 unless the feature is on."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from setuhaul.auth import local_admin, sessions
from setuhaul.auth.http import reject as _reject
from setuhaul.auth.routes import _is_secure
from setuhaul.db import open_connection
from setuhaul.identity import AuthError

if TYPE_CHECKING:
    from setuhaul.api import AppDeps

# The longest address worth looking up.
MAX_EMAIL = 254


async def read_strings(request: Request, *names: str) -> tuple[str, ...] | None:
    """The named string fields of a JSON object body, or None for anything else. Used by routes anyone can call."""
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
            return _reject(rid, 401, "BAD_CREDENTIALS")
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
