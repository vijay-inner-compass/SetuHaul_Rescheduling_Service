"""Request IDs, spans and redacted structured logging (no external service needed)."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("setuhaul")

_SECRET_KEY = re.compile(r"(key|token|secret|password|authorization|signature)", re.I)
_PHONE = re.compile(r"\+?\d[\d\s-]{8,}\d")
REDACTED = "[REDACTED]"


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    driver_id: str | None = None
    thread_id: str | None = None

    @classmethod
    def new(cls, driver_id: str | None = None, thread_id: str | None = None) -> RequestContext:
        return cls(uuid.uuid4().hex, driver_id, thread_id)


@dataclass
class Span:
    name: str
    request_id: str
    span_id: str
    parent_id: str | None
    started: float = field(default_factory=time.perf_counter)
    duration_ms: float = 0.0
    status: str = "OK"
    reason_code: str | None = None

    def set_reason(self, code: str) -> None:
        self.reason_code = code


class InMemoryExporter:
    def __init__(self) -> None:
        self.spans: list[Span] = []

    def export(self, span: Span) -> None:
        self.spans.append(span)


class _SpanScope:
    def __init__(self, span: Span, exporter: InMemoryExporter | None) -> None:
        self._span = span
        self._exporter = exporter

    def __enter__(self) -> Span:
        return self._span

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._span.duration_ms = (time.perf_counter() - self._span.started) * 1000
        if exc_type is not None:
            self._span.status = "ERROR"
        if self._exporter is not None:
            self._exporter.export(self._span)


def start_span(
    name: str,
    context: RequestContext,
    parent: Span | None = None,
    exporter: InMemoryExporter | None = None,
) -> _SpanScope:
    span = Span(name, context.request_id, uuid.uuid4().hex[:16], parent.span_id if parent else None)
    return _SpanScope(span, exporter)


def redact(value: Any) -> Any:
    """Recursively mask secret-named keys and phone-number-like substrings."""
    if isinstance(value, dict):
        return {
            k: REDACTED if isinstance(k, str) and _SECRET_KEY.search(k) else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _PHONE.sub(REDACTED, value)
    return value


def log_event(event: str, context: RequestContext, reason_code: str | None = None, **fields: Any) -> None:
    payload = {
        "event": event,
        "request_id": context.request_id,
        "driver_id": context.driver_id,
        "thread_id": context.thread_id,
        "reason_code": reason_code,
        **redact(fields),
    }
    logger.info(json.dumps(payload, default=str, sort_keys=True))
