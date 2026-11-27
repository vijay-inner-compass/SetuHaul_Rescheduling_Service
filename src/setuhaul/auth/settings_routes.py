"""Admin JSON APIs for the Configuration tab: read, save and reset settings. A secret goes in and never comes out."""

from __future__ import annotations

import math
import os
import re
import secrets
import sqlite3
import uuid
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError, field_validator
from redis.exceptions import RedisError
from starlette.concurrency import run_in_threadpool

from setuhaul import app_settings
from setuhaul.auth import audit, refusals
from setuhaul.auth.authz import require_role
from setuhaul.auth.http import reject as _reject
from setuhaul.auth.users import User
from setuhaul.db import immediate, open_connection
from setuhaul.identity import AuthError
from setuhaul.mail_transport import MailDeliveryError, OutgoingMail
from setuhaul.mailcfg import load_smtp_settings
from setuhaul.runtime_settings import MIN_TELEGRAM_SECRET_LEN
from setuhaul.settings_checks import check_bot, check_model, register_webhook

if TYPE_CHECKING:
    from setuhaul.api import AppDeps

_STATUS = {"UNKNOWN_SECTION": 404, "SETTINGS_NOT_AVAILABLE": 409, "TOO_SOON": 429}
# One test email per this many seconds, per admin.
_TEST_EVERY_S = 60
LANGSMITH_CONFIRM = "SEND_TO_LANGSMITH"
# A key travels in an HTTP header: printable, no spaces.
_Key = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500, pattern=r"^[\x21-\x7e]*$")]
_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class _Body(BaseModel):
    # hide_input_in_errors: a validation message must never repeat a pasted key.
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class ModelBody(_Body):
    provider: Literal["anthropic", "openai"] | None = None
    model_id: _Text | None = None
    api_key: _Key | None = None
    reasoning_effort: Annotated[str, StringConstraints(strip_whitespace=True, max_length=20)] | None = None
    price_in: str | None = None
    price_out: str | None = None

    @field_validator("price_in", "price_out")
    @classmethod
    def _price(cls, raw: str | None) -> str | None:
        # Empty means no price.
        if raw is None or not raw.strip():
            return None if raw is None else ""
        value = float(raw)
        if not math.isfinite(value) or value < 0:
            raise ValueError("a price is a number of 0 or more")
        return raw.strip()


class BotTestBody(_Body):
    bot_token: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200,
                                                pattern=r"^([0-9]+:[A-Za-z0-9_-]+)?$")] | None = None


class LangSmithBody(_Body):
    enabled: Literal["on", "off"] | None = None
    hide_text: Literal["on", "off"] | None = None
    api_key: _Key | None = None
    project: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None = None
    confirm: str | None = None


class TelegramBody(_Body):
    # The token becomes part of a URL, so only BotFather's shape is accepted.
    bot_token: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200,
                                                pattern=r"^([0-9]+:[A-Za-z0-9_-]+)?$")] | None = None
    webhook_secret: Annotated[str, StringConstraints(strip_whitespace=True,
                                                     pattern=r"^([A-Za-z0-9_-]{16,256})?$")] | None = None
    generate_secret: Literal["yes"] | None = None


class EmailBody(_Body):
    host: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200, pattern=r"^[A-Za-z0-9.-]+$")] | None = None
    port: Annotated[str, StringConstraints(strip_whitespace=True, max_length=10)] | None = None
    user: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    password: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None
    # Empty: send as the sending user.
    sender: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None


BODIES: dict[str, type[_Body]] = {"model": ModelBody, "langsmith": LangSmithBody, "telegram": TelegramBody,
                                  "email": EmailBody}
# A plain address: no spaces, one @, something on both sides.
_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+$")
# How Google shows an app password; the spaces are not part of it.
_APP_PASSWORD = re.compile(r"^[a-z]{4}( [a-z]{4}){3}$")


def parse_body(model: type[_Body], raw: object) -> Any:
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        # Only the field names and our own limits are passed on, nothing of the input.
        raise refusals.InvalidRequest(*refusals.problems_from(exc.errors())) from None


def model_changes(body: ModelBody, now: app_settings.Effective, base: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """(values to save, names to remove) for the model block."""
    values: dict[str, str] = {}
    remove: list[str] = []
    for field, name in (("model_id", "SETUHAUL_MODEL_ID"), ("reasoning_effort", "SETUHAUL_OPENAI_REASONING_EFFORT"),
                        ("price_in", "SETUHAUL_MODEL_PRICE_IN_PER_MTOK"), ("price_out", "SETUHAUL_MODEL_PRICE_OUT_PER_MTOK")):
        if getattr(body, field) is not None:
            values[name] = getattr(body, field)
    current = now.env.get("SETUHAUL_MODEL_PROVIDER", "")
    provider = body.provider or current
    if provider != current:
        values["SETUHAUL_MODEL_PROVIDER"] = provider
        if not body.api_key:
            # Only the .env provider already has a key to go back to.
            if provider != base.get("SETUHAUL_MODEL_PROVIDER", ""):
                raise AuthError("KEY_REQUIRED_FOR_PROVIDER")
            remove.append("SETUHAUL_MODEL_API_KEY")
    if body.api_key:
        if not provider:
            raise refusals.InvalidRequest(("provider", "is missing"))
        values["SETUHAUL_MODEL_API_KEY"] = body.api_key
        # A key is saved together with its provider, so a later .env change cannot send it elsewhere.
        values["SETUHAUL_MODEL_PROVIDER"] = provider
    return values, remove


def _langsmith_changes(body: LangSmithBody, now: app_settings.Effective) -> dict[str, str]:
    values: dict[str, str] = {}
    if body.enabled == "on":
        # The confirmed save is the owner's approval to send data.
        if body.confirm != LANGSMITH_CONFIRM:
            raise AuthError("CONFIRMATION_REQUIRED")
        values["SETUHAUL_LANGSMITH"] = "approved"
    if body.enabled is not None:
        values["SETUHAUL_LANGSMITH_ENABLED"] = "true" if body.enabled == "on" else "false"
    if body.api_key:
        values["LANGSMITH_API_KEY"] = body.api_key
    if body.project is not None:
        values["LANGSMITH_PROJECT"] = body.project
    if body.hide_text is not None:
        values["SETUHAUL_LANGSMITH_HIDE_TEXT"] = "1" if body.hide_text == "on" else "0"
    elif values and not any(now.source[n] == "saved" for n in app_settings.SECTIONS["langsmith"]):
        # The first save from the screen hides message text unless told otherwise.
        values["SETUHAUL_LANGSMITH_HIDE_TEXT"] = "1"
    return values


def _email_changes(body: EmailBody, now: app_settings.Effective) -> dict[str, str]:
    values: dict[str, str] = {}
    if body.port is not None and not (body.port.isascii() and body.port.isdigit() and 0 < int(body.port) <= 65535):
        raise AuthError("INVALID_PORT")
    for address in (body.user, body.sender):
        if address and not _ADDRESS.match(address):
            raise AuthError("INVALID_EMAIL")
    for field, name in (("host", "SETUHAUL_SMTP_HOST"), ("port", "SETUHAUL_SMTP_PORT"), ("user", "SETUHAUL_SMTP_USER"),
                        ("sender", "SETUHAUL_SMTP_FROM")):
        if getattr(body, field) is not None:
            values[name] = getattr(body, field)
    if body.password:
        password = body.password
        values["SETUHAUL_SMTP_PASSWORD"] = password.replace(" ", "") if _APP_PASSWORD.match(password) else password
    elif now.env.get("SETUHAUL_SMTP_PASSWORD") and any(
            name in values and values[name] != now.env.get(name, "")
            for name in ("SETUHAUL_SMTP_HOST", "SETUHAUL_SMTP_PORT", "SETUHAUL_SMTP_USER")):
        # A stored password is never sent to a server or account it was not typed for.
        raise AuthError("SMTP_PASSWORD_REQUIRED")
    return values


def _send_test(source: Any, env: dict[str, str], to_addr: str) -> tuple[bool, str]:
    """Send one message to the admin who asked. The result names only the kind of failure, never a credential."""
    if not env.get("SETUHAUL_SMTP_USER") or not env.get("SETUHAUL_SMTP_PASSWORD"):
        raise AuthError("EMAIL_NOT_SET_UP")
    try:
        sender = source.mail(load_smtp_settings(env))
        sender.send(OutgoingMail(
            to_addr=to_addr, subject="SetuHaul test email", message_id=f"<{uuid.uuid4().hex}@setuhaul.local>",
            body="This is a test from the SetuHaul Configuration screen. The mailbox can send.\n"))
    except MailDeliveryError as exc:
        # Already stripped to the failure's type by the transport.
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 - library text can echo credentials, so only the type is kept
        return False, type(exc).__name__
    return True, f"Sent to {to_addr}. Check the inbox and the spam folder."


def _telegram_changes(body: TelegramBody, now: app_settings.Effective) -> dict[str, str]:
    values: dict[str, str] = {}
    if body.bot_token:
        values["SETUHAUL_TELEGRAM_BOT_TOKEN"] = body.bot_token
    if body.generate_secret:
        values["SETUHAUL_TELEGRAM_WEBHOOK_SECRET"] = secrets.token_urlsafe(32)
    elif body.webhook_secret:
        values["SETUHAUL_TELEGRAM_WEBHOOK_SECRET"] = body.webhook_secret
    # A different bot (the number before the colon) must not share a secret: the old bot still calls with the old one,
    # and the webhook route trusts the secret alone.
    old_token, old_secret = (now.env.get(name, "") for name in app_settings.SECTIONS["telegram"])
    if body.bot_token and old_token and old_secret and body.bot_token.split(":")[0] != old_token.split(":")[0]:
        given = values.get("SETUHAUL_TELEGRAM_WEBHOOK_SECRET")
        if given == old_secret:
            raise AuthError("TELEGRAM_NEW_SECRET_REQUIRED")
        if given is None:
            values["SETUHAUL_TELEGRAM_WEBHOOK_SECRET"] = secrets.token_urlsafe(32)
    # Half a pair would leave the bot off: refuse rather than save it.
    after = {**now.env, **values}
    if values and not all(after.get(name) for name in app_settings.SECTIONS["telegram"]):
        raise AuthError("TELEGRAM_PAIR_REQUIRED")
    # A short secret (possible from .env) also leaves the bot off.
    if values and len(after["SETUHAUL_TELEGRAM_WEBHOOK_SECRET"]) < MIN_TELEGRAM_SECRET_LEN:
        raise AuthError("TELEGRAM_SECRET_TOO_SHORT")
    return values


def _detail(names: list[str], before: app_settings.Effective, after: app_settings.Effective) -> dict[str, object]:
    detail: dict[str, object] = {}
    for name in names:
        old, new = before.env.get(name, ""), after.env.get(name, "")
        if name in app_settings.SECRETS:
            # Never the value, only what happened to it.
            had = before.source[name] == "saved"
            detail[name] = ("changed" if had else "set") if after.source[name] == "saved" else "removed"
        else:
            detail[name] = {"from": old, "to": new}
    return detail


def _view(now: app_settings.Effective) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    for name in sorted(app_settings.EDITABLE):
        value = now.env.get(name, "")
        entry: dict[str, object] = {"source": now.source[name]}
        if name not in app_settings.SECRETS:
            entry["value"] = value
        else:
            entry["set"] = bool(value)
            # A webhook secret or mail password shows no characters at all; a short key would show most of itself.
            if name not in ("SETUHAUL_TELEGRAM_WEBHOOK_SECRET", "SETUHAUL_SMTP_PASSWORD"):
                entry["last4"] = value[-4:] if len(value) >= 8 else ""
        out[name] = entry
    return out


def settings_view(deps: AppDeps, conn: sqlite3.Connection) -> dict[str, Any]:
    """What the screen may know. Without a settings source it only shows the environment."""
    base = dict(os.environ) if deps.settings is None else deps.settings.base_env
    now = app_settings.effective(conn, base)
    token, secret = (now.env.get(n, "") for n in app_settings.SECTIONS["telegram"])
    if not deps.public_url:
        webhook = "no_public_url"
    elif token and secret and app_settings.webhook_mark(conn) == app_settings.webhook_fingerprint(token, secret, deps.public_url):
        webhook = "registered"
    else:
        webhook = "needs_registering"
    return {"settings": _view(now), "webhook": webhook, "master_key": app_settings.master_key_problem(base) or "ok",
            "unreadable": sorted(now.unreadable), "live": deps.settings is not None}


def register_settings_routes(app: FastAPI, deps: AppDeps) -> None:
    def with_conn(rid: str, fn: Any) -> Response:
        try:
            conn = open_connection(path=deps.db_path)
        except (sqlite3.Error, FileNotFoundError):
            return _reject(rid, 503, "DATABASE_UNAVAILABLE")
        try:
            return fn(conn)  # type: ignore[no-any-return]
        except AuthError as exc:
            return _reject(rid, _STATUS.get(exc.code, 422), exc.code, **refusals.details_of(exc))
        except sqlite3.Error:
            return _reject(rid, 503, "DATABASE_UNAVAILABLE")
        finally:
            conn.close()

    @app.get("/v1/admin/settings")
    async def read_settings(request: Request, user: Annotated[User, Depends(require_role("ADMIN"))]) -> Response:
        rid = request.state.request_id
        return await run_in_threadpool(with_conn, rid, lambda c: JSONResponse({**settings_view(deps, c), "request_id": rid}))

    def change(conn: sqlite3.Connection, user: User, section: str, raw: object, resetting: bool, rid: str) -> Response:
        if section not in app_settings.SECTIONS:
            raise AuthError("UNKNOWN_SECTION")
        source = deps.settings
        if source is None:
            raise AuthError("SETTINGS_NOT_AVAILABLE")
        body = None if resetting else parse_body(BODIES[section], raw)
        with immediate(conn):
            before = source.effective(conn)
            values: dict[str, str] = {}
            remove: list[str] = []
            if resetting:
                remove = [n for n in app_settings.SECTIONS[section] if before.source[n] == "saved"]
            elif isinstance(body, ModelBody):
                values, remove = model_changes(body, before, source.base_env)
            elif isinstance(body, LangSmithBody):
                values = _langsmith_changes(body, before)
            elif isinstance(body, TelegramBody):
                values = _telegram_changes(body, before)
            elif isinstance(body, EmailBody):
                values = _email_changes(body, before)
            if any(n in app_settings.SECRETS for n in values) and app_settings.master_key_problem(source.base_env):
                raise AuthError("SETTINGS_KEY_UNAVAILABLE")
            # Only what was really saved counts as removed.
            remove = [n for n in remove if before.source[n] == "saved" and n not in values]
            app_settings.reset(conn, remove)
            app_settings.save(conn, values, env=source.base_env, updated_by=user.email, now=deps.clock.now())
            names = sorted([*values, *remove])
            if names:
                audit.record(conn, actor_user_id=user.user_id, actor_email=user.email,
                             action="SETTINGS_RESET" if resetting else "SETTINGS_SAVE", target_type="settings",
                             target_id=section, outcome="OK", ip=None,
                             detail=_detail(names, before, source.effective(conn)), clock=deps.clock)
        return JSONResponse({"status": "OK", "request_id": rid})

    def in_force(rid: str) -> app_settings.Effective | Response:
        source = deps.settings
        if source is None:
            return _reject(rid, 409, "SETTINGS_NOT_AVAILABLE")
        try:
            conn = open_connection(path=deps.db_path, readonly=True)
        except (sqlite3.Error, FileNotFoundError):
            return _reject(rid, 503, "DATABASE_UNAVAILABLE")
        try:
            return source.effective(conn)
        except sqlite3.Error:
            return _reject(rid, 503, "DATABASE_UNAVAILABLE")
        finally:
            conn.close()

    def record(rid: str, user: User, action: str, section: str, ok: bool, fingerprint: str | None = None) -> Response | None:
        def write(conn: sqlite3.Connection) -> Response:
            with immediate(conn):
                if fingerprint is not None:
                    app_settings.mark_webhook(conn, fingerprint, updated_by=user.email, now=deps.clock.now())
                audit.record(conn, actor_user_id=user.user_id, actor_email=user.email, action=action, target_type="settings",
                             target_id=section, outcome="OK" if ok else "FAILED", ip=None, detail=None, clock=deps.clock)
            return Response(status_code=204)

        done = with_conn(rid, write)
        return None if done.status_code == 204 else done

    def check(user: User, what: str, raw: object, rid: str) -> Response:
        """Run one check with the values given, over those in force. Nothing is saved; the network is used with no database open."""
        now = in_force(rid)
        if isinstance(now, Response):
            return now
        source = deps.settings
        assert source is not None
        fingerprint = None
        try:
            if what == "model":
                body = parse_body(ModelBody, raw)
                values, remove = model_changes(body, now, source.base_env)
                env = {**now.env, **values}
                if "SETUHAUL_MODEL_API_KEY" in remove:
                    env["SETUHAUL_MODEL_API_KEY"] = source.base_env.get("SETUHAUL_MODEL_API_KEY", "")
                # The same rule as for a driver's message: a .env key goes only to the .env provider.
                from_env = "SETUHAUL_MODEL_API_KEY" not in values and (
                    "SETUHAUL_MODEL_API_KEY" in remove or now.source["SETUHAUL_MODEL_API_KEY"] != "saved")
                if from_env and env.get("SETUHAUL_MODEL_PROVIDER", "") != source.base_env.get("SETUHAUL_MODEL_PROVIDER", ""):
                    raise AuthError("KEY_REQUIRED_FOR_PROVIDER")
                ok, message = check_model(env, source.transport)
                action, section = "SETTINGS_TEST", "model"
            elif what == "email":
                env = {**now.env, **_email_changes(parse_body(EmailBody, raw), now)}
                try:
                    if not deps.redis.set(f"settings:testmail:{user.user_id}", 1, nx=True, ex=_TEST_EVERY_S):
                        raise AuthError("TOO_SOON")
                except RedisError:
                    # An administrator's test goes ahead when it cannot be counted.
                    pass
                ok, message = _send_test(source, env, user.email)
                action, section = "SETTINGS_TEST", "email"
            elif what == "bot":
                given = parse_body(BotTestBody, raw).bot_token
                token = given or now.env.get("SETUHAUL_TELEGRAM_BOT_TOKEN", "")
                if not token:
                    raise AuthError("TELEGRAM_PAIR_REQUIRED")
                with source.http() as http:
                    ok, message = check_bot(token, http)
                action, section = "SETTINGS_TEST", "telegram"
            else:
                token, secret = (now.env.get(n, "") for n in app_settings.SECTIONS["telegram"])
                if not deps.public_url:
                    raise AuthError("PUBLIC_URL_NOT_SET")
                if not token or not secret:
                    raise AuthError("TELEGRAM_PAIR_REQUIRED")
                # The webhook route would refuse every call made with it.
                if len(secret) < MIN_TELEGRAM_SECRET_LEN:
                    raise AuthError("TELEGRAM_SECRET_TOO_SHORT")
                with source.http() as http:
                    ok, message = register_webhook(token, secret, deps.public_url, http)
                action, section = "TELEGRAM_WEBHOOK_REGISTER", "telegram"
                if ok:
                    fingerprint = app_settings.webhook_fingerprint(token, secret, deps.public_url)
        except AuthError as exc:
            return _reject(rid, _STATUS.get(exc.code, 422), exc.code, **refusals.details_of(exc))
        failed = record(rid, user, action, section, ok, fingerprint)
        if failed is not None:
            return failed
        return JSONResponse({"ok": ok, "message": message, "request_id": rid})

    async def _json(request: Request) -> object:
        try:
            return await request.json()
        except ValueError:
            return None

    @app.post("/v1/admin/settings/model/test")
    async def test_model(request: Request, user: Annotated[User, Depends(require_role("ADMIN"))]) -> Response:
        rid = request.state.request_id
        raw = await _json(request)
        return await run_in_threadpool(check, user, "model", raw, rid)

    @app.post("/v1/admin/settings/telegram/test")
    async def test_bot(request: Request, user: Annotated[User, Depends(require_role("ADMIN"))]) -> Response:
        rid = request.state.request_id
        raw = await _json(request)
        return await run_in_threadpool(check, user, "bot", raw, rid)

    @app.post("/v1/admin/settings/email/test")
    async def test_email(request: Request, user: Annotated[User, Depends(require_role("ADMIN"))]) -> Response:
        rid = request.state.request_id
        raw = await _json(request)
        return await run_in_threadpool(check, user, "email", raw, rid)

    @app.post("/v1/admin/settings/telegram/register-webhook")
    async def register(request: Request, user: Annotated[User, Depends(require_role("ADMIN"))]) -> Response:
        rid = request.state.request_id
        return await run_in_threadpool(check, user, "webhook", None, rid)

    @app.post("/v1/admin/settings/{section}")
    async def save_settings(request: Request, section: str,
                            user: Annotated[User, Depends(require_role("ADMIN"))]) -> Response:
        rid = request.state.request_id
        raw = await _json(request)
        return await run_in_threadpool(with_conn, rid, lambda c: change(c, user, section, raw, False, rid))

    @app.post("/v1/admin/settings/{section}/reset")
    async def reset_settings(request: Request, section: str,
                             user: Annotated[User, Depends(require_role("ADMIN"))]) -> Response:
        rid = request.state.request_id
        return await run_in_threadpool(with_conn, rid, lambda c: change(c, user, section, None, True, rid))
