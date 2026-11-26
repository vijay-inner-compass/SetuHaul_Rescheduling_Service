"""Register the Telegram webhook when the service starts, if SETUHAUL_TELEGRAM_AUTO_REGISTER=1.

Off by default: a bot has one webhook, so a second copy started with the same token would take it over.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from setuhaul import app_settings
from setuhaul.auth import audit
from setuhaul.db import immediate, open_connection
from setuhaul.runtime_settings import MIN_TELEGRAM_SECRET_LEN
from setuhaul.settings_checks import register_webhook

if TYPE_CHECKING:
    from setuhaul.api import AppDeps

_LOG = logging.getLogger(__name__)


def register_at_start(deps: AppDeps) -> None:
    """Does what the Register webhook button does, with the settings in force. Never raises."""
    try:
        _register(deps)
    except Exception as error:  # noqa: BLE001 - the service must run either way; the text could quote the token
        _LOG.warning("Telegram webhook not registered: %s", type(error).__name__)


def _register(deps: AppDeps) -> None:
    source = deps.settings
    if source is None:
        return
    conn = open_connection(path=deps.db_path, readonly=True)
    try:
        env = source.effective(conn).env
    finally:
        conn.close()
    token, secret = (env.get(name, "") for name in app_settings.SECTIONS["telegram"])
    if not deps.public_url:
        _LOG.warning("Telegram webhook not registered: SETUHAUL_PUBLIC_URL is not set")
        return
    if not token or len(secret) < MIN_TELEGRAM_SECRET_LEN:
        _LOG.warning("Telegram webhook not registered: a bot token and a secret of at least %d characters are needed",
                     MIN_TELEGRAM_SECRET_LEN)
        return
    # The network is used with no database open.
    with source.http() as http:
        ok, message = register_webhook(token, secret, deps.public_url, http)
    # The message has the token and secret removed.
    _LOG.log(logging.INFO if ok else logging.WARNING, "Telegram webhook at start-up: %s", message)
    conn = open_connection(path=deps.db_path)
    try:
        with immediate(conn):
            if ok:
                app_settings.mark_webhook(conn, app_settings.webhook_fingerprint(token, secret, deps.public_url),
                                          updated_by="start-up", now=deps.clock.now())
            audit.record(conn, actor_user_id=None, actor_email=None, action="TELEGRAM_WEBHOOK_REGISTER",
                         target_type="settings", target_id="telegram", outcome="OK" if ok else "FAILED", ip=None,
                         detail={"at": "start-up"}, clock=deps.clock)
    finally:
        conn.close()
