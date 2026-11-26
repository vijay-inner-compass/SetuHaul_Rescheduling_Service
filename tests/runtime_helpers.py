"""A running API with a settings source, for the saved-settings tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from cryptography.fernet import Fernet

from setuhaul import app_settings
from setuhaul.db import open_connection

KEY = Fernet.generate_key().decode()
BASE = {"SETUHAUL_SETTINGS_KEY": KEY, "SETUHAUL_MODEL_PROVIDER": "openai", "SETUHAUL_MODEL_ID": "model-env",
        "SETUHAUL_MODEL_API_KEY": "sk-env"}


class Provider:
    """Stands in for the model provider and remembers what was sent to it."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._answer)

    def _answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Hello."}}],
                                         "usage": {"prompt_tokens": 1000, "completion_tokens": 1000}})

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


def save(path: Path, values: dict[str, str]) -> None:
    conn = open_connection(path=path)
    app_settings.save(conn, values, env=BASE, updated_by="admin@x.test", now=datetime(2026, 11, 26, tzinfo=UTC))
    conn.commit()
    conn.close()
