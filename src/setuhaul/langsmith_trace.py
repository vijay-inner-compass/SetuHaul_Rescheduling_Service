"""Opt-in LangSmith tracing of the LangGraph loop, with redaction on the way out.

Off by default and off in tests, scripts and load runs. It switches on only when BOTH are set:
    SETUHAUL_LANGSMITH=approved      (the owner's explicit approval, decision Q9 / D13)
    LANGSMITH_API_KEY=<key>          (from the environment or the git-ignored .env, never the repository)
Optional: LANGSMITH_PROJECT (default "setuhaul"), LANGSMITH_ENDPOINT (for example the EU endpoint),
SETUHAUL_LANGSMITH_HIDE_TEXT=1 (replace every message text with a length marker; use this for real driver traffic).

LangChain's own global tracing stays disabled (``disable_external_tracing``); this module attaches an explicit
tracer whose client scrubs inputs, outputs and metadata before anything is sent. A tracing failure never breaks a turn.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

APPROVAL_ENV = "SETUHAUL_LANGSMITH"
APPROVED = "approved"
TEXT_KEYS = frozenset({"content", "text", "message_text", "driver_text", "reply_text", "assistant_text", "note", "summary"})
_SECRET_KEY = re.compile(r"(key|token|secret|password|authorization|signature)", re.IGNORECASE)
_PHONE = re.compile(r"\+?\d[\d\s-]{8,}\d")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_SECRET = re.compile(r"\b(sk-[A-Za-z0-9_-]{8,}|ls[a-z]*_[A-Za-z0-9_]{16,}|[A-Fa-f0-9]{40,})\b")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def redact_text(s: str) -> str:
    """Mask emails, secret-looking strings and phone numbers; ISO dates are digits and hyphens too, so they stay."""
    s = _SECRET.sub("[SECRET]", _EMAIL.sub("[EMAIL]", s))

    def phone(m: re.Match[str]) -> str:
        t = m.group(0)
        return t if _ISO_DATE.fullmatch(t.strip()) or sum(ch.isdigit() for ch in t) < 10 else "[PHONE]"

    return _PHONE.sub(phone, s)


def _jsonable(value: Any) -> Any:
    def default(o: Any) -> Any:
        if hasattr(o, "model_dump"):
            return o.model_dump(mode="json")
        if hasattr(o, "isoformat"):
            return o.isoformat()
        return str(o)

    return json.loads(json.dumps(value, default=default))


def _walk(value: Any, hide_text: bool, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {k: "[REDACTED]" if isinstance(k, str) and _SECRET_KEY.search(k) else _walk(v, hide_text, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v, hide_text, key) for v in value]
    if isinstance(value, str):
        if hide_text and key in TEXT_KEYS:
            return f"[hidden {len(value)} chars]"
        return redact_text(value)
    return value


def scrub(value: Any, hide_text: bool = False) -> Any:
    """What may leave the machine: secrets masked, phones and emails masked, optionally all message text hidden."""
    try:
        return _walk(_jsonable(value), hide_text)
    except Exception:  # noqa: BLE001 - if it cannot be scrubbed it must not be sent
        return {"scrubbed": "[unserialisable content withheld]"}


@dataclass(frozen=True)
class TracingStatus:
    enabled: bool
    reason: str
    project: str | None = None
    hide_text: bool = False


def tracing_status(env: Mapping[str, str] | None = None) -> TracingStatus:
    e = os.environ if env is None else env
    if e.get(APPROVAL_ENV, "").strip().lower() != APPROVED:
        return TracingStatus(False, f"not opted in ({APPROVAL_ENV}=approved is not set)")
    if not e.get("LANGSMITH_API_KEY", "").strip():
        return TracingStatus(False, "opted in but LANGSMITH_API_KEY is not set")
    return TracingStatus(True, "enabled", e.get("LANGSMITH_PROJECT", "setuhaul"), e.get("SETUHAUL_LANGSMITH_HIDE_TEXT") == "1")


_cache: dict[tuple[str, str, bool, str], Any] = {}


def build_tracer(env: Mapping[str, str] | None = None, client_factory: Callable[..., Any] | None = None) -> Any | None:
    """A LangChainTracer bound to a scrubbing client, or None when tracing is off. Never raises."""
    e = os.environ if env is None else env
    status = tracing_status(e)
    if not status.enabled:
        return None
    try:
        from langchain_core.tracers.langchain import LangChainTracer

        endpoint = e.get("LANGSMITH_ENDPOINT", "")
        ck = (e["LANGSMITH_API_KEY"], status.project or "", status.hide_text, endpoint)
        if client_factory is None and ck in _cache:
            client = _cache[ck]
        else:
            if client_factory is None:
                from langsmith import Client

                client_factory = Client
            hide = status.hide_text
            kwargs: dict[str, Any] = {
                "api_key": e["LANGSMITH_API_KEY"],
                "hide_inputs": lambda d: scrub(d, hide), "hide_outputs": lambda d: scrub(d, hide), "hide_metadata": lambda d: scrub(d, hide),
            }
            if endpoint:
                kwargs["api_url"] = endpoint
            client = client_factory(**kwargs)
            if client_factory.__name__ == "Client":
                _cache[ck] = client
        return LangChainTracer(project_name=status.project, client=client, tags=["setuhaul"])
    except Exception:  # noqa: BLE001 - tracing must never take a conversation down
        return None


def flush_tracing(tracer: Any | None) -> None:
    """Short-lived scripts call this once at the end so queued runs are sent before the process exits."""
    if tracer is None:
        return
    try:
        tracer.wait_for_futures()
        tracer.client.flush()
    except Exception:  # noqa: BLE001, S110 - flushing is best effort and must never raise
        pass
