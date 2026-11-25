"""Sign-in routes: /auth/login, /auth/callback, /auth/logout, /auth/me."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

# Crypto for PKCE, the signed state cookie and constant-time compares.
import base64
import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from redis.exceptions import RedisError
from starlette.concurrency import run_in_threadpool

from setuhaul.auth import audit, local_admin, sessions, users
from setuhaul.auth.authz import AuthzError
from setuhaul.auth.http import reject as _reject
from setuhaul.db import open_connection
from setuhaul.identity import AuthError

if TYPE_CHECKING:
    from setuhaul.api import AppDeps

# The short-lived cookie that carries state, PKCE verifier and nonce.
STATE_COOKIE = "setuhaul_oauth"
_STATE_TTL_S = 600
# Hosts where the Secure cookie flag is dropped so local development works over http.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})
# Shorter secrets make the signed state cookie guessable, so Google sign-in stays off.
MIN_SECRET_BYTES = 32

_log = logging.getLogger(__name__)


def validate_public_url(url: str) -> str:
    # Empty means "derive from the request"; otherwise an absolute http(s) origin with nothing after the host.
    if not url:
        return ""
    problem = ("SETUHAUL_PUBLIC_URL must be an absolute http(s) origin such as https://haul.example.com "
               "(no path, query or trailing slash; a port, if given, is a number up to 65535)")
    try:
        parts = urlsplit(url)
        # Reading the port is what parses it: a non-numeric or out-of-range one raises here, at start-up.
        _ = parts.port
    except ValueError:
        raise ValueError(problem) from None
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.path or parts.query or parts.fragment
            or parts.username is not None or url.endswith(("?", "#"))):
        raise ValueError(problem)
    return url


def _same_bytes(a: str, b: str) -> bool:
    # Constant-time compare on UTF-8 bytes, so non-ASCII attacker input is just a mismatch, never a TypeError.
    return hmac.compare_digest(a.encode("utf-8", "surrogatepass"), b.encode("utf-8", "surrogatepass"))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(secret: bytes, body: str) -> str:
    return _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())


def _seal(secret: bytes, data: dict[str, Any]) -> str:
    # payload.signature, both base64url.
    body = _b64(json.dumps(data, sort_keys=True).encode())
    return f"{body}.{_sign(secret, body)}"


def _open(secret: bytes, cookie: str | None, now_ts: float) -> dict[str, Any] | None:
    # Any malformed, tampered or expired cookie is simply None.
    if not cookie or cookie.count(".") != 1 or not secret:
        return None
    body, sig = cookie.split(".")
    if not _same_bytes(sig, _sign(secret, body)):
        return None
    try:
        data = json.loads(_unb64(body))
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("exp"), (int, float)) or data["exp"] <= now_ts:
        return None
    return data


def _is_secure(request: Request, public_url: str = "") -> bool:
    # A configured public URL decides: Secure iff it is https, whatever the Host header says.
    if public_url:
        return urlsplit(public_url).scheme == "https"
    # Secure everywhere except plain-http local development.
    return (request.url.hostname or "") not in {h.strip("[]") for h in _LOCAL_HOSTS}


def register_auth_routes(app: FastAPI, deps: AppDeps) -> None:
    # The role guard reads the db path and clock from here.
    app.state.deps = deps
    # Refuse a malformed public URL at start-up.
    public_url = validate_public_url(deps.public_url)
    # Google login needs a strong signing key; with a short one the routes stay off.
    google_on = deps.google is not None and len(deps.session_secret) >= MIN_SECRET_BYTES
    if deps.google is not None and not google_on:
        _log.warning("Google sign-in disabled: SETUHAUL_SESSION_SECRET must be at least %d bytes", MIN_SECRET_BYTES)

    # The redirect URI Google sends the browser back to.
    def redirect_uri_for(request: Request) -> str:
        if public_url:
            return f"{public_url}/auth/callback"
        return f"{request.url.scheme}://{request.url.netloc}/auth/callback"

    # Whether cookies set for this request carry the Secure flag.
    def secure_for(request: Request) -> bool:
        return _is_secure(request, public_url)

    # A guard refusal (401, 403, 503) in the API's error shape.
    @app.exception_handler(AuthzError)
    async def authz_refused(request: Request, exc: AuthzError) -> Response:
        return _reject(getattr(request.state, "request_id", ""), exc.status, exc.code)

    # Run a function on a fresh connection; database trouble becomes 503.
    def with_conn(rid: str, fn: Any) -> Response:
        try:
            conn = open_connection(path=deps.db_path)
        except (sqlite3.Error, FileNotFoundError):
            return _reject(rid, 503, "DATABASE_UNAVAILABLE")
        try:
            return fn(conn)  # type: ignore[no-any-return]
        except sqlite3.Error:
            return _reject(rid, 503, "DATABASE_UNAVAILABLE")
        finally:
            conn.close()

    # Record a refused login (no user row, no session) and answer with the reason.
    def refuse(request: Request, rid: str, status: int, code: str, email: str | None = None) -> Response:
        def write(conn: sqlite3.Connection) -> Response:
            audit.record(conn, actor_user_id=None, actor_email=email, action="LOGIN", target_type=None, target_id=None,
                         outcome="LOGIN_REFUSED", ip=request.client.host if request.client else None,
                         detail={"reason": code}, clock=deps.clock)
            conn.commit()
            return _reject(rid, status, code)

        resp = with_conn(rid, write)
        # The state is spent whatever happened.
        resp.delete_cookie(STATE_COOKIE, path="/")
        return resp

    @app.get("/auth/login")
    async def login(request: Request) -> Response:
        rid = request.state.request_id
        if deps.google is None or not google_on:
            return _reject(rid, 503, "GOOGLE_NOT_CONFIGURED")
        # Fresh single-use values for this attempt.
        state, verifier, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(48), secrets.token_urlsafe(24)
        # PKCE S256 challenge.
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        redirect_uri = redirect_uri_for(request)
        resp = RedirectResponse(deps.google.authorize_url(state, challenge, redirect_uri, nonce), status_code=302)
        # The signed cookie lets the callback recover them without server-side storage.
        cookie = _seal(deps.session_secret, {"state": state, "verifier": verifier, "nonce": nonce,
                                             "exp": deps.clock.now().timestamp() + _STATE_TTL_S})
        resp.set_cookie(STATE_COOKIE, cookie, max_age=_STATE_TTL_S, **sessions.cookie_kwargs(secure_for(request)))  # type: ignore[arg-type]
        return resp

    @app.get("/auth/callback")
    async def callback(request: Request, code: str | None = None, state: str | None = None,
                       error: str | None = None) -> Response:
        rid = request.state.request_id
        if deps.google is None or not google_on:
            return _reject(rid, 503, "GOOGLE_NOT_CONFIGURED")
        # Google itself refused (for example access_denied): a fixed reason, never Google's text.
        if error is not None:
            return await run_in_threadpool(refuse, request, rid, 400, "GOOGLE_ERROR")
        # Validate the signed cookie and that the returned state matches it.
        data = _open(deps.session_secret, request.cookies.get(STATE_COOKIE), deps.clock.now().timestamp())
        if data is None or not code or not state or not _same_bytes(str(data.get("state", "")), state):
            return await run_in_threadpool(refuse, request, rid, 400, "BAD_STATE")
        # Single use: the first presenter of this state wins; a replay finds the marker already set.
        # The Redis call is synchronous, so it runs in the threadpool; an outage is an audited 503.
        try:
            fresh = await run_in_threadpool(deps.redis.set, f"auth:state:{state}", "1", nx=True, ex=_STATE_TTL_S)
        except RedisError:
            return await run_in_threadpool(refuse, request, rid, 503, "STATE_STORE_UNAVAILABLE")
        if not fresh:
            return await run_in_threadpool(refuse, request, rid, 400, "BAD_STATE")
        try:
            # The Google round trip is blocking network I/O.
            ident = await run_in_threadpool(
                deps.google.exchange_and_verify, code, data["verifier"],
                redirect_uri_for(request), data["nonce"])
        except AuthError as exc:
            return await run_in_threadpool(refuse, request, rid, 401, exc.code)
        if not ident.email_verified:
            return await run_in_threadpool(refuse, request, rid, 403, "EMAIL_NOT_VERIFIED", ident.email)

        def finish(conn: sqlite3.Connection) -> Response:
            # Invite-only: an unknown address never gets a row.
            user = users.find_by_email(conn, ident.email)
            if user is None:
                return refuse_in(conn, 403, "NOT_INVITED")
            if user.status != "ACTIVE":
                return refuse_in(conn, 403, "DISABLED")
            token = sessions.create(conn, user, "GOOGLE", deps.clock)
            audit.record(conn, actor_user_id=user.user_id, actor_email=user.email, action="LOGIN", target_type=None,
                         target_id=None, outcome="LOGIN_OK", ip=request.client.host if request.client else None,
                         detail=None, clock=deps.clock)
            conn.commit()
            resp = RedirectResponse("/", status_code=302)
            resp.set_cookie(sessions.COOKIE_NAME, token, max_age=8 * 3600, **sessions.cookie_kwargs(secure_for(request)))  # type: ignore[arg-type]
            resp.delete_cookie(STATE_COOKIE, path="/")
            return resp

        # Audit and answer a refusal on the connection already open.
        def refuse_in(conn: sqlite3.Connection, status: int, reason: str) -> Response:
            audit.record(conn, actor_user_id=None, actor_email=ident.email, action="LOGIN", target_type=None,
                         target_id=None, outcome="LOGIN_REFUSED", ip=request.client.host if request.client else None,
                         detail={"reason": reason}, clock=deps.clock)
            conn.commit()
            resp = _reject(rid, status, reason)
            resp.delete_cookie(STATE_COOKIE, path="/")
            return resp

        return await run_in_threadpool(with_conn, rid, finish)

    @app.post("/auth/local-admin")
    async def local_admin_login(request: Request) -> Response:
        rid = request.state.request_id
        # The feature is invisible unless switched on.
        if not deps.local_admin_enabled:
            return _reject(rid, 404, "NOT_FOUND")
        # A malformed body is just bad credentials; it must not reveal anything.
        try:
            body = await request.json()
            email, password = str(body["email"]), str(body["password"])
            # Refuse over-long input before any hashing work.
            if len(password) > local_admin.MAX_LENGTH:
                return _reject(rid, 401, "BAD_CREDENTIALS")
        except (ValueError, KeyError, TypeError):
            return _reject(rid, 401, "BAD_CREDENTIALS")
        ip = request.client.host if request.client else None

        def attempt(conn: sqlite3.Connection) -> Response:
            # verify audits every outcome itself; scrypt makes this blocking, so it runs in the threadpool.
            try:
                user = local_admin.verify(conn, email, password, deps.clock, ip)
            except AuthError:
                # A locked account looks exactly like a wrong password, so callers cannot enumerate admins.
                return _reject(rid, 401, "BAD_CREDENTIALS")
            # One-hour session, cookie max-age to match.
            token = sessions.create(conn, user, "LOCAL_ADMIN", deps.clock)
            conn.commit()
            resp = JSONResponse({"status": "OK", "email": user.email, "request_id": rid})
            resp.set_cookie(sessions.COOKIE_NAME, token, max_age=3600, **sessions.cookie_kwargs(secure_for(request)))  # type: ignore[arg-type]
            return resp

        return await run_in_threadpool(with_conn, rid, attempt)

    @app.post("/auth/logout")
    async def logout(request: Request) -> Response:
        rid = request.state.request_id
        token = request.cookies.get(sessions.COOKIE_NAME)

        def write(conn: sqlite3.Connection) -> Response:
            if token:
                user = sessions.resolve(conn, token, deps.clock)
                sessions.revoke(conn, token, deps.clock)
                if user is not None:
                    audit.record(conn, actor_user_id=user.user_id, actor_email=user.email, action="LOGOUT",
                                 target_type=None, target_id=None, outcome="OK",
                                 ip=request.client.host if request.client else None, detail=None, clock=deps.clock)
                    conn.commit()
            resp = JSONResponse({"status": "OK", "request_id": rid})
            resp.delete_cookie(sessions.COOKIE_NAME, path="/")
            return resp

        return await run_in_threadpool(with_conn, rid, write)

    @app.get("/auth/me")
    async def me(request: Request) -> Response:
        rid = request.state.request_id
        token = request.cookies.get(sessions.COOKIE_NAME)

        def read(conn: sqlite3.Connection) -> Response:
            user = sessions.resolve(conn, token, deps.clock) if token else None
            if user is None:
                return JSONResponse({"status": "REJECTED", "code": "UNAUTHENTICATED", "reason_code": "UNAUTHENTICATED",
                                     "request_id": rid}, status_code=401)
            return JSONResponse({"email": user.email, "role": user.role, "display_name": user.display_name,
                                 "facilities": list(user.facilities), "request_id": rid})

        return await run_in_threadpool(with_conn, rid, read)
