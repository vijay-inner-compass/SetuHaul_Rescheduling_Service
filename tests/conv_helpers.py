"""Helpers for driving the conversation workflow with scripted models."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import fakeredis

from setuhaul.booking import make_hold_checker
from setuhaul.clock import FakeClock
from setuhaul.config import load_settings
from setuhaul.conversation import (
    ConversationReply,
    ConversationServices,
    InboundMessage,
    TrustedConversationContext,
    handle_message,
)
from setuhaul.escalation import InMemoryEscalationSink, JsonlEscalationSink
from setuhaul.identity import TrustedDriverContext, normalize_phone
from setuhaul.memory import ConversationMemoryStore
from setuhaul.model_gateway import (
    ModelMessage,
    ModelPolicy,
    ModelResponse,
    ScriptedGateway,
    Step,
    ToolCall,
)

POLICY = ModelPolicy(provider="scripted", model_id="scripted")


def tc(name: str, call_id: str = "t1", **args: Any) -> ModelResponse:
    return ModelResponse(tool_calls=(ToolCall(id=call_id, name=name, arguments=args),))


def tcs(*calls: tuple[str, dict[str, Any]]) -> ModelResponse:
    return ModelResponse(tool_calls=tuple(ToolCall(id=f"t{i}", name=n, arguments=a) for i, (n, a) in enumerate(calls, 1)))


def tc_dyn(name: str, fn: Callable[[Sequence[ModelMessage]], dict[str, Any]], call_id: str = "t1") -> Step:
    return lambda msgs: ModelResponse(tool_calls=(ToolCall(id=call_id, name=name, arguments=fn(msgs)),))


def say_text(text: str) -> ModelResponse:
    return ModelResponse(text=text)


def facts_from(messages: Sequence[ModelMessage]) -> dict[str, Any]:
    marker = "not by the driver)\n"
    return json.loads(messages[0].content.split(marker, 1)[1])  # type: ignore[no-any-return]


def tool_json(messages: Sequence[ModelMessage], name: str) -> dict[str, Any]:
    for m in reversed(messages):
        if m.role == "tool" and m.name == name:
            return json.loads(m.content)  # type: ignore[no-any-return]
    raise LookupError(name)


def trusted_for(conn: sqlite3.Connection, driver_id: str, thread_id: str) -> TrustedConversationContext:
    r = conn.execute("select carrier_id, phone from drivers where driver_id=?", (driver_id,)).fetchone()
    return TrustedConversationContext(
        driver=TrustedDriverContext(driver_id=driver_id, carrier_id=r["carrier_id"], phone_e164=normalize_phone(r["phone"])),
        thread_id=thread_id)


class Chat:
    """One driver in one thread. Each ``say`` installs a fresh scripted model for that message."""

    def __init__(self, db: sqlite3.Connection, clock: FakeClock, tmp_path: Path, driver: str = "DRV006", thread: str = "THR001") -> None:
        self.db, self.clock = db, clock
        self.redis = fakeredis.FakeRedis(decode_responses=True)
        self.memory = ConversationMemoryStore(db, self.redis, clock, load_settings(), hold_checker=make_hold_checker(db, clock))
        self.sink = InMemoryEscalationSink()
        self.fallback_path = tmp_path / "fallback-escalations.jsonl"
        self.fallback = JsonlEscalationSink(self.fallback_path)
        self.trusted = trusted_for(db, driver, thread)
        self.driver, self.thread = driver, thread
        self.gateway: ScriptedGateway = ScriptedGateway([])
        self.n = 0
        from setuhaul.observability import InMemoryExporter

        self.exporter = InMemoryExporter()

    def say(self, text: str, steps: Sequence[Step], message_id: str | None = None, policy: ModelPolicy = POLICY) -> ConversationReply:
        self.n += 1
        self.gateway = ScriptedGateway(list(steps))
        services = ConversationServices(conn=self.db, memory=self.memory, gateway=self.gateway, policy=policy,
                                        sink=self.sink, fallback_sink=self.fallback, exporter=self.exporter)
        return handle_message(InboundMessage(message_id=message_id or f"MSG-{self.driver}-{self.n}", text=text),
                              self.trusted, self.clock, services)

    @property
    def model_calls(self) -> int:
        return len(self.gateway.calls)

    def tools_offered(self, call: int = 0) -> tuple[str, ...]:
        return self.gateway.calls[call][1]

    def last_messages(self) -> tuple[ModelMessage, ...]:
        return self.gateway.calls[-1][0]

    def rows(self, sql: str, *args: Any) -> list[tuple[Any, ...]]:
        return [tuple(r) for r in self.db.execute(sql, args)]

    def held(self) -> int:
        return int(self.db.execute("select count(*) from appointments where appointment_status='HELD'").fetchone()[0])
