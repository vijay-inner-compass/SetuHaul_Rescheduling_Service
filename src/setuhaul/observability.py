"""Request IDs, spans and redacted structured logging (no external service needed)."""

# Lazy annotations.
from __future__ import annotations

# json renders log lines.
import json

# logging is the standard logger.
import logging

# re for the redaction patterns.
import re

# time.perf_counter measures span durations.
import time

# uuid generates request and span ids.
import uuid

# dataclass/field for the value types.
from dataclasses import dataclass, field

# Any for loosely typed log fields.
from typing import Any

# The service's logger name; deployments attach their own handlers to it.
logger = logging.getLogger("setuhaul")

# Any dictionary key containing one of these words has its value masked before logging.
_SECRET_KEY = re.compile(r"(key|token|secret|password|authorization|signature)", re.IGNORECASE)
# A run of 10+ digits (with spaces or dashes) looks like a phone number and is masked.
_PHONE = re.compile(r"\+?\d[\d\s-]{8,}\d")
# What masked values are replaced with.
REDACTED = "[REDACTED]"


# Who and what a request is about; carried through the call chain so every log line can be tied together.
@dataclass(frozen=True)
class RequestContext:
    request_id: str
    driver_id: str | None = None
    thread_id: str | None = None

    @classmethod
    def new(cls, driver_id: str | None = None, thread_id: str | None = None) -> RequestContext:
        # A fresh random request id (32 hex characters) for each inbound message.
        return cls(uuid.uuid4().hex, driver_id, thread_id)


# One timed step of a request (for example "model.loop").
@dataclass
class Span:
    name: str
    request_id: str
    span_id: str
    # The enclosing span's id, if any, to reconstruct nesting.
    parent_id: str | None
    # perf_counter is a monotonic clock, immune to the system time changing mid-request.
    started: float = field(default_factory=time.perf_counter)
    duration_ms: float = 0.0
    status: str = "OK"
    # A short code explaining why a span ended abnormally (for example ROUND_CAP).
    reason_code: str | None = None

    def set_reason(self, code: str) -> None:
        self.reason_code = code


# Collects finished spans in memory (tests and the demo read them; production would export elsewhere).
class InMemoryExporter:
    def __init__(self) -> None:
        self.spans: list[Span] = []

    def export(self, span: Span) -> None:
        self.spans.append(span)


# The "with" wrapper that times a span and exports it when the block ends.
class _SpanScope:
    def __init__(self, span: Span, exporter: InMemoryExporter | None) -> None:
        self._span = span
        self._exporter = exporter

    def __enter__(self) -> Span:
        # Entering the block hands the span to the caller so it can set a reason code.
        return self._span

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        # Duration in milliseconds from creation to exit.
        self._span.duration_ms = (time.perf_counter() - self._span.started) * 1000
        # An exception inside the block marks the span as an error (the exception itself still propagates).
        if exc_type is not None:
            self._span.status = "ERROR"
        # Hand the finished span to the exporter, if one is configured.
        if self._exporter is not None:
            self._exporter.export(self._span)


def start_span(
    name: str,
    context: RequestContext,
    parent: Span | None = None,
    exporter: InMemoryExporter | None = None,
) -> _SpanScope:
    # A short random span id; the parent's id is recorded when this span is nested inside another.
    span = Span(name, context.request_id, uuid.uuid4().hex[:16], parent.span_id if parent else None)
    return _SpanScope(span, exporter)


def redact(value: Any) -> Any:
    """Recursively mask secret-named keys and phone-number-like substrings."""
    # Dictionaries: mask the whole value of any secret-named key, otherwise recurse into the value.
    if isinstance(value, dict):
        return {
            k: REDACTED if isinstance(k, str) and _SECRET_KEY.search(k) else redact(v)
            for k, v in value.items()
        }
    # Lists and tuples: redact each element (tuples become lists).
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    # Strings: mask anything that looks like a phone number.
    if isinstance(value, str):
        return _PHONE.sub(REDACTED, value)
    # Numbers, booleans and None pass through.
    return value


def log_event(event: str, context: RequestContext, reason_code: str | None = None, **fields: Any) -> None:
    # One JSON object per line: easy to search and to ship to a log system.
    payload = {
        "event": event,
        "request_id": context.request_id,
        "driver_id": context.driver_id,
        "thread_id": context.thread_id,
        "reason_code": reason_code,
        # Extra fields are redacted before they are logged, so a secret or phone number cannot leak by accident.
        **redact(fields),
    }
    # sort_keys makes identical events produce identical lines; default=str renders dates and other objects as text.
    logger.info(json.dumps(payload, default=str, sort_keys=True))
