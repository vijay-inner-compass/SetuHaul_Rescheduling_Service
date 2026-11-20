"""Opt-in LangSmith tracing of the LangGraph loop, with redaction on the way out.

Off by default and off in tests, scripts and load runs. It switches on only when BOTH are set:
    SETUHAUL_LANGSMITH=approved      (the owner's explicit approval, decision Q9 / D13)
    LANGSMITH_API_KEY=<key>          (from the environment or the git-ignored .env, never the repository)
Optional: LANGSMITH_PROJECT (default "setuhaul"), LANGSMITH_ENDPOINT (for example the EU endpoint),
SETUHAUL_LANGSMITH_HIDE_TEXT=1 (replace every message text with a length marker; use this for real driver traffic).

LangChain's own global tracing stays disabled (``disable_external_tracing``); this module attaches an explicit
tracer whose client scrubs inputs, outputs and metadata before anything is sent. A tracing failure never breaks a turn.
"""

# Lazy annotations.
from __future__ import annotations

# json round-trips values into plain data before scrubbing.
import json

# os reads the environment.
import os

# re finds secrets, phone numbers and emails.
import re

# Typing helpers.
from collections.abc import Callable, Mapping

# dataclass for the status record.
from dataclasses import dataclass

# Typing helper.
from typing import Any

# The environment variable the owner must set to approve tracing.
APPROVAL_ENV = "SETUHAUL_LANGSMITH"
# The only value that counts as approval.
APPROVED = "approved"
# Field names that hold message text; with HIDE_TEXT on these are replaced by a length marker.
TEXT_KEYS = frozenset({"content", "text", "message_text", "driver_text", "reply_text", "assistant_text", "note", "summary"})
# Any key containing one of these words has its value masked.
_SECRET_KEY = re.compile(r"(key|token|secret|password|authorization|signature)", re.IGNORECASE)
# Phone-number-like digit runs.
_PHONE = re.compile(r"\+?\d[\d\s-]{8,}\d")
# Email addresses.
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# API-key-like strings (sk-..., LangSmith keys, long hex).
_SECRET = re.compile(r"\b(sk-[A-Za-z0-9_-]{8,}|ls[a-z]*_[A-Za-z0-9_]{16,}|[A-Fa-f0-9]{40,})\b")
# Dates look like digit runs, so they are recognised and kept.
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


# Mask emails, secrets and phone numbers in a string.
def redact_text(s: str) -> str:
    """Mask emails, secret-looking strings and phone numbers; ISO dates are digits and hyphens too, so they stay."""
    # Mask emails first, then secrets.
    s = _SECRET.sub("[SECRET]", _EMAIL.sub("[EMAIL]", s))

    # Decide whether a digit run is really a phone number.
    def phone(m: re.Match[str]) -> str:
        # The matched text.
        t = m.group(0)
        # Dates and runs with fewer than 10 digits stay; otherwise mask.
        return t if _ISO_DATE.match(t.strip()) or sum(ch.isdigit() for ch in t) < 10 else "[PHONE]"

    # Apply to every candidate.
    return _PHONE.sub(phone, s)


# Convert any value (models, dates, odd objects) into plain JSON data.
def _jsonable(value: Any) -> Any:
    # How to encode things JSON does not know.
    def default(o: Any) -> Any:
        # Pydantic models.
        if hasattr(o, "model_dump"):
            return o.model_dump(mode="json")
        # Dates and times.
        if hasattr(o, "isoformat"):
            return o.isoformat()
        # Anything else becomes text.
        return str(o)

    # Round-trip through JSON text to get plain data.
    return json.loads(json.dumps(value, default=default))


# Walk the data and scrub every string and secret-named key.
def _walk(value: Any, hide_text: bool, key: str | None = None) -> Any:
    # Dicts: mask whole values of secret-named keys, recurse into the rest.
    if isinstance(value, dict):
        return {k: "[REDACTED]" if isinstance(k, str) and _SECRET_KEY.search(k) else _walk(v, hide_text, k) for k, v in value.items()}
    # Lists: scrub each element.
    if isinstance(value, list):
        return [_walk(v, hide_text, key) for v in value]
    # Strings: hide or redact.
    if isinstance(value, str):
        # With hide_text on, message text is replaced by its length.
        if hide_text and key in TEXT_KEYS:
            return f"[hidden {len(value)} chars]"
        # Otherwise mask personal data.
        return redact_text(value)
    # Numbers, booleans and None pass through.
    return value


# What may leave the machine: secrets, phones and emails masked, optionally all message text hidden.
def scrub(value: Any, hide_text: bool = False) -> Any:
    """What may leave the machine: secrets masked, phones and emails masked, optionally all message text hidden."""
    # Scrub; if that is impossible, send nothing.
    try:
        # The scrubbed copy.
        return _walk(_jsonable(value), hide_text)
    # If scrubbing fails, withhold the content entirely.
    except Exception:  # noqa: BLE001 - if it cannot be scrubbed it must not be sent
        # A placeholder instead of the content.
        return {"scrubbed": "[unserialisable content withheld]"}


# The tracing decision.
@dataclass(frozen=True)
class TracingStatus:
    # Is tracing on?
    enabled: bool
    # Why it is on or off.
    reason: str
    # The LangSmith project name.
    project: str | None = None
    # Hide all message text?
    hide_text: bool = False


# Decide whether tracing is on. It needs BOTH the owner's approval and an API key.
def tracing_status(env: Mapping[str, str] | None = None) -> TracingStatus:
    # Use the real environment unless a test supplies one.
    e = os.environ if env is None else env
    # No approval: off, whatever else is set.
    if e.get(APPROVAL_ENV, "").strip().lower() != APPROVED:
        return TracingStatus(False, f"not opted in ({APPROVAL_ENV}=approved is not set)")
    # Approved but no key: off, and says why.
    if not e.get("LANGSMITH_API_KEY", "").strip():
        return TracingStatus(False, "opted in but LANGSMITH_API_KEY is not set")
    # On: default project 'setuhaul'; message text hidden if requested.
    return TracingStatus(True, "enabled", e.get("LANGSMITH_PROJECT", "setuhaul"), e.get("SETUHAUL_LANGSMITH_HIDE_TEXT") == "1")


# Clients are cached per (key, project, hide flag, endpoint) so one is not built per message.
_cache: dict[tuple[str, str, bool, str], Any] = {}


# A LangChain tracer bound to a scrubbing client, or None when tracing is off. Never raises.
def build_tracer(env: Mapping[str, str] | None = None, client_factory: Callable[..., Any] | None = None) -> Any | None:
    """A LangChainTracer bound to a scrubbing client, or None when tracing is off. Never raises."""
    e = os.environ if env is None else env
    # Is it enabled?
    status = tracing_status(e)
    # Off: no tracer.
    if not status.enabled:
        # No tracer.
        return None
    try:
        # Imported only when tracing is actually used.
        from langchain_core.tracers.langchain import LangChainTracer

        # An optional custom LangSmith endpoint (for example the EU one).
        endpoint = e.get("LANGSMITH_ENDPOINT", "")
        # The cache key.
        ck = (e["LANGSMITH_API_KEY"], status.project or "", status.hide_text, endpoint)
        # Reuse a cached client.
        if client_factory is None and ck in _cache:
            # The cached client.
            client = _cache[ck]
        else:
            # Use the real LangSmith client unless a test supplied a stand-in.
            if client_factory is None:
                # The real client class.
                from langsmith import Client

                client_factory = Client
            # Capture the flag for the scrubbing functions.
            hide = status.hide_text
            # The client is built with hooks that scrub inputs, outputs AND metadata BEFORE anything is sent.
            kwargs: dict[str, Any] = {
                "api_key": e["LANGSMITH_API_KEY"],
                "hide_inputs": lambda d: scrub(d, hide), "hide_outputs": lambda d: scrub(d, hide), "hide_metadata": lambda d: scrub(d, hide),
            }
            # Add the endpoint only if set.
            if endpoint:
                kwargs["api_url"] = endpoint
            # Build the client.
            client = client_factory(**kwargs)
            # Cache only real clients.
            if client_factory.__name__ == "Client":
                _cache[ck] = client
        # The tracer that will be attached to the model loop.
        return LangChainTracer(project_name=status.project, client=client, tags=["setuhaul"])
    # Tracing must never take a conversation down: on any failure, run without it.
    except Exception:  # noqa: BLE001 - tracing must never take a conversation down
        return None


# Short-lived scripts call this once at the end so queued runs are sent before the process exits.
def flush_tracing(tracer: Any | None) -> None:
    """Short-lived scripts call this once at the end so queued runs are sent before the process exits."""
    # Nothing to flush.
    if tracer is None:
        return
    try:
        # Wait for background uploads.
        tracer.wait_for_futures()
        # Push anything still queued.
        tracer.client.flush()
    # Flushing is best effort and must never raise.
    except Exception:  # noqa: BLE001, S110 - flushing is best effort and must never raise
        pass
