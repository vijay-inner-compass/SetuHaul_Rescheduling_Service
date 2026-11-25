"""The local demo's separate driver account: send messages as one driver, and see only that driver's conversation.

Registered only by ``build_demo_app`` (never by ``create_app``), and only for loopback requests. In the real service a driver
is not a web user: they write from Telegram. This stands in for the driver's phone, so it deliberately has its own cookie,
is not a ``users`` row, and reaches no dock board or operations route.
"""

# Lazy annotations.
from __future__ import annotations

# hashlib: only the hash of the demo driver's token is kept.
import hashlib

# secrets: unguessable tokens.
import secrets

# Callable describes the two functions shared with the console.
from collections.abc import Callable

# Path of the page file.
from pathlib import Path

# Any: loosely typed JSON.
from typing import Any

# FastAPI pieces.
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

# Audit, and the same eligibility rule the Telegram bot uses.
from setuhaul.auth import audit
from setuhaul.auth.onboarding import driver_eligible
from setuhaul.db import IST, open_connection

# The demo driver's own cookie, deliberately not the web session cookie: both can be signed in in one browser.
COOKIE_NAME = "setuhaul_demo_driver"
# Who /demo/driver-login signs in as when no ?driver= is given (the console's default driver too).
DEFAULT_DRIVER_ID = "DRV006"
# Strictly this machine, both as the peer and as the name the page was reached under (blocks DNS rebinding).
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


class DriverMessageIn(BaseModel):
    # No driver_id field at all: who is speaking comes from the cookie, never from the request.
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _reject(status: int, code: str) -> JSONResponse:
    # http_status is in the body too, because the page reads it from there.
    return JSONResponse({"http_status": status, "status": "REJECTED", "reason_code": code}, status_code=status)


def register_demo_driver(app: FastAPI, deps: Any, send: Callable[[str, str, str], dict[str, Any]],
                         view: Callable[[str], dict[str, Any]]) -> None:
    """Add the driver account's routes. ``send`` and ``view`` are the console's own message pipeline and conversation reader."""
    # token hash -> driver id. In memory only: restarting the demo signs the demo driver out.
    signed_in: dict[str, str] = {}
    html = (Path(__file__).parent / "demo" / "driver.html").read_text(encoding="utf-8")

    def local(request: Request) -> bool:
        host = request.client.host if request.client else ""
        return host in LOOPBACK and (request.url.hostname or "") in LOOPBACK

    def profile(driver_id: str) -> dict[str, Any] | None:
        # The driver's name and carrier, or None when they may not use the service (unknown, suspended, licence expired).
        conn = open_connection(path=deps.demo.db_path, readonly=True)
        try:
            if not driver_eligible(conn, driver_id, deps.clock.now().astimezone(IST).date()):
                return None
            row = conn.execute("select d.driver_name, ca.carrier_name from drivers d join carriers ca using(carrier_id) where d.driver_id=?",
                               (driver_id,)).fetchone()
        finally:
            conn.close()
        return None if row is None else {"id": driver_id, "name": row["driver_name"], "carrier": row["carrier_name"]}

    def who(request: Request) -> dict[str, Any] | None:
        # Only the demo driver cookie counts, and eligibility is re-checked on every request.
        if not local(request):
            return None
        driver_id = signed_in.get(_hash(request.cookies.get(COOKIE_NAME, "")))
        return None if driver_id is None else profile(driver_id)

    @app.get("/demo/driver-login")
    async def driver_login(request: Request, driver: str = DEFAULT_DRIVER_ID) -> Any:
        if not local(request):
            return _reject(403, "DEMO_IS_LOCAL_ONLY")

        def go() -> Any:
            if profile(driver) is None:
                return _reject(404, "UNKNOWN_DRIVER")
            conn = open_connection(path=deps.demo.db_path)
            try:
                audit.record(conn, actor_user_id=None, actor_email=None, action="DEMO_DRIVER_LOGIN", target_type="driver",
                             target_id=driver, outcome="OK", ip=request.client.host if request.client else None, detail=None, clock=deps.clock)
                conn.commit()
            finally:
                conn.close()
            token = secrets.token_urlsafe(32)
            signed_in[_hash(token)] = driver
            resp = RedirectResponse("/demo/driver", status_code=302)
            # Strict and scoped to the driver pages: another site cannot make the browser send it.
            resp.set_cookie(COOKIE_NAME, token, max_age=8 * 3600, httponly=True, samesite="strict", path="/demo/driver")
            return resp

        return await run_in_threadpool(go)

    @app.get("/demo/driver", response_class=HTMLResponse)
    async def driver_page(request: Request) -> Any:
        if await run_in_threadpool(who, request) is None:
            return _reject(401, "UNAUTHENTICATED")
        return HTMLResponse(html)

    @app.get("/demo/driver/api/state")
    async def driver_state(request: Request) -> Any:
        def go() -> Any:
            me = who(request)
            if me is None:
                return _reject(401, "UNAUTHENTICATED")
            return {"driver": me, "chat_enabled": deps.demo.live_factory is not None, **view(me["id"])}

        return await run_in_threadpool(go)

    @app.post("/demo/driver/api/message")
    async def driver_message(request: Request) -> Any:
        raw = await request.body()

        def go() -> Any:
            me = who(request)
            if me is None:
                return _reject(401, "UNAUTHENTICATED")
            # Parsed only after the caller is known. JSON only, so a plain cross-site form cannot post here.
            if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
                return _reject(415, "JSON_REQUIRED")
            try:
                body = DriverMessageIn.model_validate_json(raw)
            except ValidationError:
                return _reject(422, "INVALID_REQUEST")
            if deps.demo.live_factory is None:
                return _reject(409, "NO_MODEL_CONFIGURED")
            return send(me["id"], body.text, request.state.request_id)

        return await run_in_threadpool(go)
