"""The settings in force for the running service: saved values over the environment, read afresh each time."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

import httpx

from setuhaul import app_settings
from setuhaul.model_gateway import (
    PROVIDERS,
    ModelGateway,
    ModelPolicy,
    UnconfiguredGateway,
    build_gateway,
    load_model_policy,
)


class SettingsSource:
    """Hands out the settings in force. Only the real service has one; tests and the demo run without."""

    def __init__(self, base_env: Mapping[str, str], stub: bool = False, transport: httpx.BaseTransport | None = None) -> None:
        # A copy of the environment as the service started.
        self.base_env = dict(base_env)
        self.stub = stub
        # A stand-in network for tests.
        self._transport = transport

    def __repr__(self) -> str:
        # base_env holds secrets.
        return "SettingsSource()"

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
        return build_gateway(policy, now.env, self._transport), policy
