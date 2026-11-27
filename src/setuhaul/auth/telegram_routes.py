"""POST /v1/telegram/webhook: the driver channel over Telegram, bound to a driver by shared contact."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import hmac
import json
import logging
import sqlite3
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from setuhaul import passes
from setuhaul.auth import audit, telegram
from setuhaul.db import open_connection
from setuhaul.events import queue_driver_notice
from setuhaul.gps import active_shipment_for_driver
from setuhaul.identity import AuthError, TrustedDriverContext
from setuhaul.language import say

if TYPE_CHECKING:
    from setuhaul.api import AppDeps, MessageIn

_LOG = logging.getLogger(__name__)
# The pipeline shared with /v1/driver/messages, for a driver whose identity is already verified.
Pipeline = Callable[[sqlite3.Connection, TrustedDriverContext, "MessageIn", str], JSONResponse]
# The engine's input limit; longer Telegram texts get a polite refusal instead of an error.
MAX_TEXT = 2000


def _int(value: object) -> int | None:
    # Telegram ids are integers; booleans are not ids.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def register_telegram_routes(app: FastAPI, deps: AppDeps, pipeline: Pipeline) -> None:
    def send(client: telegram.TelegramClient | None, chat_id: int, text: str, button: bool = False) -> bool:
        # A failed send must not turn into a Telegram retry storm: log the class only (never the token) and carry on.
        if client is None:
            _LOG.warning("telegram reply dropped: no client configured")
            return False
        try:
            client.send(chat_id, text, request_contact_button=button)
        except telegram.TelegramSendError as exc:
            _LOG.warning("telegram send failed: %s", exc)
            return False
        return True

    def reply(conn: sqlite3.Connection, client: telegram.TelegramClient | None, chat_id: int, driver_id: str, text: str) -> None:
        # An answer to a linked driver is not lost when Telegram refuses it: it joins the queue the notice sender retries.
        if send(client, chat_id, text):
            return
        shipment = active_shipment_for_driver(conn, driver_id)
        if shipment is not None and queue_driver_notice(conn, driver_id, shipment, text, deps.clock.now()):
            return
        # The queue hangs off an appointment; with none to hang it on, leave a trace for an admin.
        audit.record(conn, actor_user_id=None, actor_email=None, action="TELEGRAM_REPLY_DROPPED", target_type="driver",
                     target_id=driver_id, outcome="FAILED", ip=None, detail=None, clock=deps.clock)
        conn.commit()

    def handle(client: telegram.TelegramClient | None, update: dict[str, Any], update_id: int, rid: str) -> None:
        msg = update.get("message")
        if not isinstance(msg, dict):
            return
        chat, sender = msg.get("chat"), msg.get("from")
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            return
        # Only private chats are served.
        chat_id, user_id = _int(chat.get("id")), _int(sender.get("id"))
        if chat.get("type") != "private" or chat_id is None or user_id is None or sender.get("is_bot") is True:
            return
        from setuhaul.api import MessageIn

        # A database that cannot be opened raises to the route, which answers 503 so Telegram retries later.
        conn = open_connection(path=deps.db_path)
        try:
            # The IST date from the injected clock decides licence validity.
            today = deps.clock.now().date()
            driver_id = telegram.resolve_driver(conn, user_id, today)
            contact = msg.get("contact")
            if driver_id is None:
                if isinstance(contact, dict):
                    result = telegram.bind_from_contact(
                        conn, telegram_user_id=user_id, chat_id=chat_id, contact_user_id=_int(contact.get("user_id")),
                        contact_phone=str(contact.get("phone_number", "")), clock=deps.clock)
                    send(client, chat_id, telegram.BOUND_REPLY if result == telegram.BindResult.BOUND else telegram.NEUTRAL_REFUSAL)
                    bound = telegram.resolve_driver(conn, user_id, today) if result == telegram.BindResult.BOUND else None
                    if bound is not None:
                        passes.send_waiting(conn, client, bound, deps.clock)
                elif telegram.has_binding(conn, user_id):
                    # Bound once but revoked or no longer eligible: the neutral refusal, nothing reaches the engine.
                    send(client, chat_id, telegram.NEUTRAL_REFUSAL)
                else:
                    send(client, chat_id, telegram.SHARE_PROMPT, button=True)
                return
            text = msg.get("text")
            # A photo, voice note, location or sticker cannot be read: say so rather than stay silent.
            if not isinstance(text, str) or not text.strip():
                reply(conn, client, chat_id, driver_id, say("unsupported_content", passes.driver_language(conn, driver_id)))
                return
            if len(text) > MAX_TEXT:
                reply(conn, client, chat_id, driver_id, telegram.TOO_LONG_REPLY)
                return
            try:
                trusted = telegram.trusted_context(conn, driver_id)
                # The update id (prefixed, so it cannot collide with the HMAC channel's ids) is the message id, so Telegram's retry of the same update is deduplicated.
                payload = MessageIn(sender=trusted.phone_e164, message_id=f"tg:{update_id}", text=text)  # type: ignore[call-arg]
            except (AuthError, ValidationError):
                send(client, chat_id, telegram.NEUTRAL_REFUSAL)
                return
            resp = pipeline(conn, trusted, payload, rid)
            body = json.loads(bytes(resp.body))
            if resp.status_code != 200:
                _LOG.info("telegram message not answered: HTTP %s", resp.status_code)
                # Another delivery of this same message is being worked on and will answer it.
                if body.get("reason_code") == "IN_PROGRESS":
                    return
                lang = passes.driver_language(conn, driver_id)
                if resp.status_code == 429:
                    answer = say("slow_down", lang, seconds=str(body.get("retry_after_seconds") or 60))
                else:
                    answer = say("not_processed", lang)
                reply(conn, client, chat_id, driver_id, answer)
                return
            # A replayed duplicate was already answered the first time.
            if not body.get("duplicate") and body.get("reply"):
                reply(conn, client, chat_id, driver_id, str(body["reply"]))
        finally:
            conn.close()

    @app.post("/v1/telegram/webhook")
    async def telegram_webhook(request: Request) -> JSONResponse:
        rid = request.state.request_id
        # Without a configured secret the route never accepts anything.
        from setuhaul.api import (
            MAX_TELEGRAM_BODY_BYTES,
            PayloadTooLarge,
            read_body_capped,
            telegram_in_force,
        )

        # Read once, so one request cannot mix an old secret with a new bot.
        try:
            secret, client = await run_in_threadpool(telegram_in_force, deps)
        except (sqlite3.Error, FileNotFoundError):
            return JSONResponse({"status": "REJECTED", "reason_code": "DATABASE_UNAVAILABLE", "request_id": rid}, status_code=503)
        if not secret:
            return JSONResponse({"status": "REJECTED", "reason_code": "TELEGRAM_NOT_CONFIGURED", "request_id": rid}, status_code=503)
        # The secret is checked before the body is read or parsed.
        given = request.headers.get("x-telegram-bot-api-secret-token", "").encode()
        if not hmac.compare_digest(given, secret):
            return JSONResponse({"status": "REJECTED", "reason_code": "INVALID_SECRET", "request_id": rid}, status_code=401)
        try:
            raw = await read_body_capped(request, MAX_TELEGRAM_BODY_BYTES)
        except PayloadTooLarge:
            return JSONResponse({"status": "REJECTED", "reason_code": "PAYLOAD_TOO_LARGE", "request_id": rid}, status_code=413)
        try:
            update = json.loads(raw)
        except ValueError:
            return JSONResponse({"status": "REJECTED", "reason_code": "INVALID_REQUEST", "request_id": rid}, status_code=400)
        update_id = _int(update.get("update_id")) if isinstance(update, dict) else None
        if update_id is None:
            return JSONResponse({"status": "REJECTED", "reason_code": "INVALID_REQUEST", "request_id": rid}, status_code=400)
        try:
            await run_in_threadpool(handle, client, update, update_id, rid)
        except (sqlite3.Error, FileNotFoundError):
            return JSONResponse({"status": "REJECTED", "reason_code": "DATABASE_UNAVAILABLE", "request_id": rid}, status_code=503)
        # Always 200 for a well-formed update, so Telegram does not retry.
        return JSONResponse({"status": "OK", "request_id": rid})
