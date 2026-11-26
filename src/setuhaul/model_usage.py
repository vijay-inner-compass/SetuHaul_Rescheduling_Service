"""Record each model call, and the driver message it belongs to, for the admin Model tab.

A recorder is made per message. It wraps the gateway, collects the calls in memory and saves them after the turn, so
recording never takes part in the conversation: a failure to save is logged and otherwise ignored.
"""

# Lazy annotations.
from __future__ import annotations

# Standard logger.
import logging

# The database connection type.
import sqlite3

# perf_counter times the calls.
import time

# Random row ids.
import uuid

# Callable and Sequence types.
from collections.abc import Callable, Sequence

# Value type for one call.
from dataclasses import dataclass

# Timestamp type.
from datetime import datetime

# Loose typing for tool schemas.
from typing import Any

# The injectable clock.
from setuhaul.clock import Clock

# One atomic write.
from setuhaul.db import immediate

# The gateway interface and its errors.
from setuhaul.model_gateway import (
    ModelGateway,
    ModelMessage,
    ModelPolicy,
    ModelResponse,
    ModelTimeout,
    ModelUnavailable,
    PermanentModelError,
    TransientModelError,
)

# The scripted stand-in is labelled as such.
from setuhaul.stub_model import StubGateway

# The service's logger.
logger = logging.getLogger("setuhaul")


# The provider and model id to record for this gateway.
def describe_model(gateway: ModelGateway, policy: ModelPolicy) -> tuple[str, str]:
    # The stand-in is not the configured model, whatever the settings say.
    if isinstance(gateway, StubGateway):
        return ("stand-in", "scripted stand-in")
    # Matched by name: the class lives in api.py, which imports this module.
    if type(gateway).__name__ == "UnconfiguredGateway":
        return ("", "")
    return (policy.provider, policy.model_id)


# One attempt at the model.
@dataclass(frozen=True)
class _Call:
    at: datetime
    provider: str
    model_id: str
    duration_ms: float
    outcome: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None


# The outcome code for a failed call; the most specific class is checked first.
def _outcome(error: BaseException) -> str:
    if isinstance(error, ModelTimeout):
        return "TIMEOUT"
    if isinstance(error, TransientModelError):
        return "TRANSIENT"
    if isinstance(error, PermanentModelError):
        return "REFUSED"
    if isinstance(error, ModelUnavailable) and error.code == "MODEL_NOT_CONFIGURED":
        return "NOT_CONFIGURED"
    return "ERROR"


# A gateway that times each attempt and otherwise behaves exactly like the one inside.
class _RecordingGateway:
    def __init__(self, inner: ModelGateway, recorder: TurnRecorder) -> None:
        self._inner = inner
        self._recorder = recorder

    def complete(self, messages: Sequence[ModelMessage], tools: Sequence[dict[str, Any]], policy: ModelPolicy) -> ModelResponse:
        rec = self._recorder
        provider, model_id = describe_model(self._inner, policy)
        at, started = rec.clock.now(), rec.timer()
        try:
            response = self._inner.complete(messages, tools, policy)
        # Record the failure, then let the very same exception carry on.
        except BaseException as error:
            rec.calls.append(_Call(at, provider, model_id, (rec.timer() - started) * 1000, _outcome(error)))
            raise
        # A cost only when a price is set.
        priced = policy.price_in_per_mtok > 0 or policy.price_out_per_mtok > 0
        rec.calls.append(_Call(at, provider, model_id, (rec.timer() - started) * 1000, "OK", response.input_tokens,
                               response.output_tokens, policy.estimate_cost(response) if priced else None))
        return response


# Collects the calls of one driver message and saves them.
class TurnRecorder:
    def __init__(self, clock: Clock, timer: Callable[[], float] = time.perf_counter) -> None:
        self.clock = clock
        self.timer = timer
        self.calls: list[_Call] = []
        # When handling this message began.
        self._at = clock.now()
        self._started = timer()

    def wrap(self, gateway: ModelGateway) -> ModelGateway:
        return _RecordingGateway(gateway, self)

    def save(self, conn: sqlite3.Connection, status: str, reason_code: str | None) -> None:
        # A message that never reached the model is not a model message.
        if not self.calls:
            return
        duration = (self.timer() - self._started) * 1000
        turn_id = f"MT-{uuid.uuid4().hex}"
        try:
            with immediate(conn):
                conn.execute("insert into model_turns values (?,?,?,?,?,?)",
                             (turn_id, self._at.isoformat(), self._at.timestamp(), duration, status, reason_code))
                conn.executemany(
                    "insert into model_calls values (?,?,?,?,?,?,?,?,?,?,?)",
                    [(f"MC-{uuid.uuid4().hex}", turn_id, c.at.isoformat(), c.at.timestamp(), c.provider, c.model_id, c.duration_ms,
                      c.outcome, c.input_tokens, c.output_tokens, c.cost_usd) for c in self.calls])
        # Missing tables, a locked file, anything: the driver's reply matters more than the record.
        except Exception as error:  # noqa: BLE001 - recording must never fail a driver's message
            logger.warning("model_usage.save_failed: %s", type(error).__name__)
