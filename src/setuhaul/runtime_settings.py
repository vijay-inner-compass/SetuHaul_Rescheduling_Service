"""The settings in force for the running service: saved values over the environment, read afresh each time."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping

import httpx

from setuhaul import app_settings
from setuhaul.auth.telegram import RealTelegramClient, TelegramClient
from setuhaul.model_gateway import (
    PROVIDERS,
    ModelGateway,
    ModelPolicy,
    UnconfiguredGateway,
    build_gateway,
    load_model_policy,
)

# A shorter Telegram webhook secret is too easy to guess.
MIN_TELEGRAM_SECRET_LEN = 16


def _real_telegram(token: str) -> TelegramClient:
    return RealTelegramClient(token, httpx.Client(timeout=10.0))


class SettingsSource:
    """Hands out the settings in force. Only the real service has one; tests and the demo run without."""

    def __init__(self, base_env: Mapping[str, str], stub: bool = False, transport: httpx.BaseTransport | None = None,
                 telegram_factory: Callable[[str], TelegramClient] | None = None) -> None:
        # A copy of the environment as the service started.
        self.base_env = dict(base_env)
        self.stub = stub
        # A stand-in network for tests.
        self._transport = transport
        self._telegram_factory = telegram_factory or _real_telegram
        # The client for the token last in force, so one is not built for every message.
        self._bot: tuple[str, TelegramClient] | None = None

    def __repr__(self) -> str:
        # base_env holds secrets.
        return "SettingsSource()"

    def http(self) -> httpx.Client:
        """A client for the checks run from the screen; the caller closes it."""
        return httpx.Client(timeout=10.0, transport=self._transport)

    @property
    def transport(self) -> httpx.BaseTransport | None:
        return self._transport

    def effective(self, conn: sqlite3.Connection) -> app_settings.Effective:
        return app_settings.effective(conn, self.base_env)

    def model(self, conn: sqlite3.Connection) -> tuple[ModelGateway, ModelPolicy]:
        now = self.effective(conn)
        policy = load_model_policy(now.env)
        if self.stub:
            from setuhaul.stub_model import StubGateway

            return StubGateway(), policy
        # No provider, an unknown one, or a saved key that cannot be read: the safe outage path.
        if policy.provider not in PROVIDERS or "SETUHAUL_MODEL_API_KEY" in now.unreadable:
            return UnconfiguredGateway(), policy
        # A key from .env belongs to the provider named there; never send it to a different saved one.
        if now.source["SETUHAUL_MODEL_API_KEY"] != "saved" and policy.provider != self.base_env.get("SETUHAUL_MODEL_PROVIDER", ""):
            return UnconfiguredGateway(), policy
        return build_gateway(policy, now.env, self._transport), policy

    def telegram(self, conn: sqlite3.Connection) -> tuple[bytes, TelegramClient | None]:
        """The webhook secret and reply client in force; (b"", None) leaves Telegram off."""
        env = self.effective(conn).env
        secret = env.get("SETUHAUL_TELEGRAM_WEBHOOK_SECRET", "")
        token = env.get("SETUHAUL_TELEGRAM_BOT_TOKEN", "")
        # An unreadable saved value is empty here, so it also lands in this branch.
        if not token or len(secret) < MIN_TELEGRAM_SECRET_LEN:
            return b"", None
        bot = self._bot
        if bot is None or bot[0] != token:
            bot = self._bot = (token, self._telegram_factory(token))
        return secret.encode(), bot[1]
