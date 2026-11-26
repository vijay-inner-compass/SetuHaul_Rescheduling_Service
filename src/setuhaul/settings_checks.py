"""Checks run from the Configuration tab. Each returns (worked, words for the admin) with no secret in the words."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import httpx

from setuhaul.app_settings import SECRETS
from setuhaul.model_gateway import (
    PROVIDERS,
    ModelMessage,
    ModelTimeout,
    ModelUnavailable,
    PermanentModelError,
    TransientModelError,
    build_gateway,
    load_model_policy,
)

_TELEGRAM = "https://api.telegram.org/bot"


def _clean(message: str, *hidden: str) -> str:
    # Another service's words could repeat what was sent to it.
    for value in hidden:
        if value:
            message = message.replace(value, "[hidden]")
    return message[:300]


def check_model(env: Mapping[str, str], transport: httpx.BaseTransport | None = None) -> tuple[bool, str]:
    policy = load_model_policy(env)
    if policy.provider not in PROVIDERS or not policy.model_id or not env.get("SETUHAUL_MODEL_API_KEY"):
        return False, "Provider, model ID and API key are all needed."
    messages = [ModelMessage(role="system", content="This is a connection test."),
                ModelMessage(role="user", content="Reply with the word OK.")]
    try:
        build_gateway(policy, env, transport).complete(messages, [], policy)
    except ModelTimeout:
        return False, "The provider did not answer in time."
    except (PermanentModelError, TransientModelError) as error:
        # A network failure's text may quote the request: give its kind only.
        cause = error.__cause__
        if isinstance(cause, httpx.HTTPError):
            return False, f"Could not reach the provider ({type(cause).__name__})."
        return False, _clean(str(error), *(env.get(n, "") for n in SECRETS))
    except ModelUnavailable as error:
        return False, error.code
    except Exception as error:  # noqa: BLE001 - an outside service must never break the screen
        return False, f"The test failed ({type(error).__name__})."
    return True, f"{policy.provider} / {policy.model_id} answered."


def _telegram(http: httpx.Client, token: str, method: str, body: dict[str, Any] | None, *hidden: str) -> tuple[bool, Any]:
    # httpx logs every request URL at INFO, and the URL carries the token.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        response = http.post(f"{_TELEGRAM}{token}/{method}", json=body or {})
        data = response.json()
        if response.status_code >= 300 or not data.get("ok"):
            return False, _clean(f"Telegram refused: {data.get('description') or response.status_code}", token, *hidden)
        return True, data.get("result")
    except Exception as error:  # noqa: BLE001 - the message could quote the URL, which holds the token
        return False, f"Could not reach Telegram ({type(error).__name__})."


def check_bot(token: str, http: httpx.Client) -> tuple[bool, str]:
    ok, result = _telegram(http, token, "getMe", None)
    return (True, f"@{result.get('username', '')}") if ok else (False, result)


def register_webhook(token: str, secret: str, public_url: str, http: httpx.Client) -> tuple[bool, str]:
    address = f"{public_url.rstrip('/')}/v1/telegram/webhook"
    ok, result = _telegram(http, token, "setWebhook", {"url": address, "secret_token": secret}, secret)
    if not ok:
        return False, result
    ok, info = _telegram(http, token, "getWebhookInfo", None, secret)
    if not ok:
        return True, f"Registered {address}. Telegram's status could not be read."
    last = _clean(str(info.get("last_error_message") or "none"), token, secret)
    return True, f"Registered {info.get('url', address)}: {info.get('pending_update_count', 0)} waiting, last error: {last}."
