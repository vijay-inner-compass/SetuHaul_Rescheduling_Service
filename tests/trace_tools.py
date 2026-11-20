"""Record scripted conversations as traces (regression baselines, never evidence about a model)."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from setuhaul.clock import FakeClock
from setuhaul.conversation import ConversationServices, InboundMessage, handle_message
from setuhaul.evaluation import RecordingGateway, Trace, build_turn_trace, sanitize_trace
from setuhaul.model_gateway import ScriptedGateway, Step
from tests.conv_helpers import POLICY, Chat


class TraceChat(Chat):
    def __init__(self, db: sqlite3.Connection, clock: FakeClock, tmp_path: Path, scenario: str, driver: str = "DRV006", thread: str = "THR001") -> None:
        super().__init__(db, clock, tmp_path, driver=driver, thread=thread)
        self.scenario = scenario
        self.clock_start = clock.now().isoformat()
        self.turn_traces: list = []  # type: ignore[type-arg]

    def say(self, text: str, steps: Sequence[Step], message_id: str | None = None, advance_s: int = 0, **_: object):  # type: ignore[no-untyped-def, override]
        self.clock.advance(timedelta(seconds=advance_s))
        self.n += 1
        mid = message_id or f"MSG-{self.driver}-{self.n}"
        recorder = RecordingGateway(ScriptedGateway(list(steps)))
        self.gateway = recorder._inner  # type: ignore[assignment]
        services = ConversationServices(conn=self.db, memory=self.memory, gateway=recorder, policy=POLICY, sink=self.sink,
                                        fallback_sink=self.fallback, exporter=self.exporter)
        before = len(self.sink.requests)
        reply = handle_message(InboundMessage(message_id=mid, text=text), self.trusted, self.clock, services)
        keys = list(self.sink.requests[-1].context) if len(self.sink.requests) > before else []
        self.turn_traces.append(build_turn_trace(self.n, mid, text, recorder.drain(), reply, keys, advance_s))
        return reply

    def trace(self) -> Trace:
        raw = Trace(trace_id=f"scripted-{self.scenario}", source="scripted", scenario=self.scenario, driver_id=self.driver,
                    thread_id=self.thread, clock_start=self.clock_start, turns=self.turn_traces)
        return sanitize_trace(raw)
