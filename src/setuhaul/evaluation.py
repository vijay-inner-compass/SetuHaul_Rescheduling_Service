"""Trace capture, sanitising, failure taxonomy, deterministic evaluators and offline replay.

A trace records one conversation: what the driver wrote, what the model asked for (tools and
arguments), what the deterministic layers answered, and what the driver was told. Evaluators are
plain functions over a trace, so they run in CI without a model. Replay feeds the *recorded model
outputs* back through the real deterministic layers and compares what they produce; it never calls
a live model.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from setuhaul.clock import FakeClock
from setuhaul.config import load_settings
from setuhaul.conversation import (
    ConversationReply,
    ConversationServices,
    InboundMessage,
    TrustedConversationContext,
    handle_message,
)
from setuhaul.db import parse_operational_timestamp
from setuhaul.escalation import InMemoryEscalationSink, JsonlEscalationSink
from setuhaul.identity import TrustedDriverContext, normalize_phone
from setuhaul.integrity import InvariantViolation, check_invariants
from setuhaul.language import claims_confirmed, claims_held
from setuhaul.memory import ConversationMemoryStore
from setuhaul.model_gateway import (
    ModelGateway,
    ModelMessage,
    ModelPolicy,
    ModelResponse,
    ModelTimeout,
    PermanentModelError,
    ScriptedGateway,
    ToolCall,
    TransientModelError,
)

SCHEMA_VERSION = 1
WRITE_TOOLS = {"request_hold", "confirm_reschedule", "release_pending_change", "cancel_appointment"}
COMMITTED = {"HELD", "CONFIRMED", "RELEASED", "CANCELLED"}
REJECTIONS = {"TOOL_NOT_ALLOWED", "INVALID_ARGUMENTS", "APPROVAL_NOT_PRESENT", "ONE_WRITE_PER_MESSAGE"}
HANDOFF_KEYS = {"language", "tools_executed", "recent_turns", "selected_shipment_id"}


# ---- trace model ---------------------------------------------------------------------------------------
class ToolEvent(BaseModel):
    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    executed: bool | None = None
    error: str | None = None


class RoundTrace(BaseModel):
    offered_tools: list[str] = Field(default_factory=list)
    assistant_text: str = ""
    model_error: str | None = None
    tool_events: list[ToolEvent] = Field(default_factory=list)


class TurnTrace(BaseModel):
    turn: int
    message_id: str
    advance_s: int = 0
    driver_text: str
    rounds: list[RoundTrace] = Field(default_factory=list)
    reply_text: str = ""
    reply_status: str = ""
    escalation_reason: str | None = None
    handoff_context_keys: list[str] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)
    verified_hold: bool = False
    verified_confirmed: bool = False
    loop_status: str | None = None


class Trace(BaseModel):
    schema_version: int = SCHEMA_VERSION
    trace_id: str
    source: Literal["live", "scripted"]  # a scripted trace is a regression baseline, NOT evidence about a model
    model: dict[str, str] | None = None
    scenario: str
    world: str = "snapshot + TEST_FIXTURE labour/consignee evidence"
    driver_id: str
    thread_id: str
    clock_start: str
    turns: list[TurnTrace] = Field(default_factory=list)


# ---- recording -----------------------------------------------------------------------------------------
@dataclass
class RecordedCall:
    messages: tuple[ModelMessage, ...]
    offered: tuple[str, ...]
    response: ModelResponse | None
    error: str | None = None


class RecordingGateway:
    """Wraps any gateway and keeps what it saw and answered. Works for live and scripted models."""

    def __init__(self, inner: ModelGateway) -> None:
        self._inner = inner
        self.calls: list[RecordedCall] = []

    def complete(self, messages: Sequence[ModelMessage], tools: Sequence[dict[str, Any]], policy: ModelPolicy) -> ModelResponse:
        offered = tuple(t["name"] for t in tools)
        try:
            resp = self._inner.complete(messages, tools, policy)
        except Exception as exc:
            self.calls.append(RecordedCall(tuple(messages), offered, None, f"{type(exc).__name__}: {str(exc)[:200]}"))
            raise
        self.calls.append(RecordedCall(tuple(messages), offered, resp))
        return resp

    def drain(self) -> list[RecordedCall]:
        out, self.calls = self.calls, []
        return out


def _parse(content: str) -> Any:
    try:
        return json.loads(content)
    except ValueError:
        return content


def build_turn_trace(
    turn: int, message_id: str, driver_text: str, calls: list[RecordedCall], reply: ConversationReply,
    handoff_context_keys: Sequence[str] = (), advance_s: int = 0,
) -> TurnTrace:
    rounds: list[RoundTrace] = []
    facts: dict[str, Any] = {}
    if calls:
        marker = "not by the driver)\n"
        sys_msg = calls[0].messages[0].content
        if marker in sys_msg:
            f = json.loads(sys_msg.split(marker, 1)[1])
            facts = {"live_hold": f.get("live_hold") is not None, "has_option_set": f.get("latest_option_set_id") is not None}
    for i, c in enumerate(calls):
        events: list[ToolEvent] = []
        if c.response is not None:
            later = calls[i + 1].messages if i + 1 < len(calls) else ()
            for tcall in c.response.tool_calls:
                out = next((m.content for m in later if m.role == "tool" and m.tool_call_id == tcall.id), None)
                parsed = _parse(out) if out is not None else None
                err = parsed.get("error") if isinstance(parsed, dict) else None
                events.append(ToolEvent(call_id=tcall.id, name=tcall.name, arguments=dict(tcall.arguments), result=parsed,
                                        executed=None if out is None else err not in REJECTIONS, error=err))
        rounds.append(RoundTrace(offered_tools=list(c.offered), assistant_text=c.response.text if c.response else "",
                                 model_error=c.error, tool_events=events))
    return TurnTrace(turn=turn, message_id=message_id, advance_s=advance_s, driver_text=driver_text, rounds=rounds,
                     reply_text=reply.text, reply_status=reply.status, escalation_reason=reply.escalation_reason,
                     handoff_context_keys=list(handoff_context_keys), facts=facts, verified_hold=reply.verified_hold,
                     verified_confirmed=reply.verified_confirmed, loop_status=reply.loop_status)


# ---- sanitising ----------------------------------------------------------------------------------------
_ID_PATTERNS = (("OPT", re.compile(r"OPT-[0-9A-F]{12}"), "$OPT"), ("HOLD", re.compile(r"APT-H-[0-9A-F]{10}"), "$HOLD"),
                ("CASE", re.compile(r"ESC-[0-9A-F]{8}"), "$CASE"))
_PHONE = re.compile(r"\+?\d[\d\s-]{8,}\d")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_SECRET = re.compile(r"\b(sk-[A-Za-z0-9_-]{8,}|[A-Fa-f0-9]{40,})\b")


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _redact_phone(m: re.Match[str]) -> str:
    """Digit runs that are phone numbers go; ISO dates (also digits and hyphens) stay."""
    s = m.group(0)
    if _ISO_DATE.fullmatch(s.strip()) or sum(ch.isdigit() for ch in s) < 10:
        return s
    return "[PHONE]"


def sanitize_trace(trace: Trace) -> Trace:
    """Replace run-specific ids with stable placeholders and redact phone numbers, emails and secrets."""
    text = trace.model_dump_json()
    for _, pattern, prefix in _ID_PATTERNS:
        seen: dict[str, str] = {}
        for m in pattern.finditer(text):
            seen.setdefault(m.group(0), f"{prefix}{len(seen) + 1}")
        for real, token in seen.items():
            text = text.replace(real, token)
    text = _EMAIL.sub("[EMAIL]", text)
    text = _SECRET.sub("[SECRET]", text)
    text = _PHONE.sub(_redact_phone, text)
    return Trace.model_validate_json(text)


# ---- failure taxonomy ----------------------------------------------------------------------------------
def classify_failure(turn: TurnTrace) -> list[str]:
    """Group what went wrong in a turn: INPUT, TRAJECTORY, DECISION, DEPENDENCY, TECHNICAL_OUTCOME.
    An empty list means nothing went wrong. INSUFFICIENT_EVIDENCE means the trace cannot say."""
    out: list[str] = []
    errors = {e.error for r in turn.rounds for e in r.tool_events if e.error}
    if turn.reply_status == "TEMPORARILY_UNAVAILABLE" or any(r.model_error for r in turn.rounds) or \
            turn.escalation_reason in ("MODEL_UNAVAILABLE", "DATABASE_UNAVAILABLE"):
        out.append("DEPENDENCY")
    if turn.reply_status == "LANGUAGE_PROMPT" or turn.escalation_reason == "LANGUAGE_UNSUPPORTED":
        out.append("INPUT")
    if errors & {"TOOL_NOT_ALLOWED", "ONE_WRITE_PER_MESSAGE"} or turn.loop_status in ("ROUND_CAP", "TOOL_CAP", "DEADLINE", "BUDGET"):
        out.append("TRAJECTORY")
    if errors & {"APPROVAL_NOT_PRESENT", "NOT_LATEST_OPTION_SET", "FOREIGN_SHIPMENT", "FOREIGN_OPTION_SET", "FOREIGN_HOLD"} or \
            any(isinstance(e.result, dict) and e.result.get("status") == "NOT_LATEST_OPTION_SET" for r in turn.rounds for e in r.tool_events):
        out.append("DECISION")
    if "INVALID_ARGUMENTS" in errors:
        out.append("TECHNICAL_OUTCOME")
    if not turn.reply_text.strip():
        out.append("INSUFFICIENT_EVIDENCE")
    return out


# ---- deterministic evaluators --------------------------------------------------------------------------
@dataclass(frozen=True)
class Finding:
    evaluator: str
    turn: int | None
    ok: bool
    detail: str = ""


def _norm(s: str) -> str:
    return " ".join(s.casefold().split())


def _events(turn: TurnTrace) -> list[ToolEvent]:
    return [e for r in turn.rounds for e in r.tool_events]


def eval_identity(trace: Trace) -> list[Finding]:
    """The model never supplies an identity: any attempt must have been rejected, never executed."""
    out = []
    for t in trace.turns:
        for e in _events(t):
            if {"driver_id", "thread_id"} & set(e.arguments):
                out.append(Finding("identity", t.turn, e.executed is False and e.error == "INVALID_ARGUMENTS",
                                   f"{e.name} carried an identity field"))
    return out or [Finding("identity", None, True, "no tool call carried an identity field")]


def eval_tool_order(trace: Trace) -> list[Finding]:
    out: list[Finding] = []
    searched = held_turn = None
    for t in trace.turns:
        ev = _events(t)
        committed_writes = [e for e in ev if e.name in WRITE_TOOLS and e.executed and isinstance(e.result, dict) and e.result.get("status") in COMMITTED]
        out.append(Finding("tool_order", t.turn, len(committed_writes) <= 1, f"{len(committed_writes)} committed writes in one message"))
        for e in ev:
            if e.executed and e.name in ("search_slot_options", "offer_original_slot") and isinstance(e.result, dict) and e.result.get("option_set_id"):
                searched = t.turn
            if e.executed and e.name == "request_hold" and isinstance(e.result, dict) and e.result.get("status") == "HELD":
                out.append(Finding("tool_order", t.turn, searched is not None, "hold needs options shown first"))
                held_turn = t.turn
            if e.executed and e.name == "confirm_reschedule" and isinstance(e.result, dict) and e.result.get("status") == "CONFIRMED":
                out.append(Finding("tool_order", t.turn, held_turn is not None and held_turn < t.turn,
                                   "confirmation needs a hold from an earlier message"))
    return out


def eval_explicit_approval(trace: Trace) -> list[Finding]:
    out = []
    for t in trace.turns:
        for e in _events(t):
            if e.name in WRITE_TOOLS and e.executed and isinstance(e.result, dict) and e.result.get("status") in COMMITTED:
                q = _norm(str(e.arguments.get("driver_quote", "")))
                out.append(Finding("explicit_approval", t.turn, len(q) >= 2 and q in _norm(t.driver_text),
                                   f"{e.name}: quote {q!r} in driver text"))
    return out or [Finding("explicit_approval", None, True, "no committed write in this trace")]


def eval_option_set_ownership(trace: Trace) -> list[Finding]:
    out, known = [], set()
    for t in trace.turns:
        for e in _events(t):
            if isinstance(e.result, dict) and e.result.get("option_set_id") and e.name in ("search_slot_options", "offer_original_slot"):
                known.add(e.result["option_set_id"])
            if e.name in ("request_hold", "compare_option_set") and e.executed:
                ident = e.arguments.get("option_set_id")
                bad = isinstance(e.result, dict) and e.result.get("error") == "FOREIGN_OPTION_SET"
                out.append(Finding("option_set_ownership", t.turn, ident in known and not bad,
                                   f"{e.name} used {ident}; known to this thread: {sorted(known)}"))
    return out or [Finding("option_set_ownership", None, True, "no option set was used")]


def eval_state_backed_wording(trace: Trace) -> list[Finding]:
    out = []
    for t in trace.turns:
        read_confirmed = any(e.name == "get_shipment_status" and isinstance(e.result, dict) and
                             (e.result.get("appointment") or {}).get("status") == "CONFIRMED" for e in _events(t))
        if claims_confirmed(t.reply_text):
            out.append(Finding("state_backed_wording", t.turn, t.verified_confirmed or read_confirmed,
                               "reply claims a confirmation"))
        if claims_held(t.reply_text):
            out.append(Finding("state_backed_wording", t.turn, t.verified_hold or t.facts.get("live_hold", False),
                               "reply claims a hold"))
    return out or [Finding("state_backed_wording", None, True, "no booking claim in any reply")]


def eval_round_caps(trace: Trace) -> list[Finding]:
    p = load_settings()
    return [Finding("round_caps", t.turn,
                    len(t.rounds) <= p.model_max_rounds and sum(1 for e in _events(t) if e.executed) <= p.model_max_tool_calls,
                    f"{len(t.rounds)} rounds, {sum(1 for e in _events(t) if e.executed)} executed tools") for t in trace.turns]


def eval_handoff_complete(trace: Trace) -> list[Finding]:
    out = [Finding("handoff_complete", t.turn, HANDOFF_KEYS <= set(t.handoff_context_keys) or t.escalation_reason in ("LANGUAGE_UNSUPPORTED", "DATABASE_UNAVAILABLE"),
                   f"context keys {sorted(t.handoff_context_keys)}") for t in trace.turns if t.reply_status == "HANDOFF"]
    return out or [Finding("handoff_complete", None, True, "no hand-off in this trace")]


EVALUATORS: tuple[Callable[[Trace], list[Finding]], ...] = (
    eval_identity, eval_tool_order, eval_explicit_approval, eval_option_set_ownership, eval_state_backed_wording,
    eval_round_caps, eval_handoff_complete,
)


def run_evaluators(trace: Trace) -> list[Finding]:
    return [f for ev in EVALUATORS for f in ev(trace)]


# ---- replay ---------------------------------------------------------------------------------------------
@dataclass
class Mismatch:
    turn: int
    field: str
    expected: Any
    actual: Any


@dataclass
class ReplayResult:
    trace: Trace  # what the deterministic layers produced this time (sanitised)
    mismatches: list[Mismatch] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    violations: list[InvariantViolation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches and not self.violations and all(f.ok for f in self.findings)


_PLACEHOLDER = re.compile(r"\$(OPT|HOLD|CASE)(\d+)")


def _scan_ids(messages: Sequence[ModelMessage], ids: dict[str, str]) -> None:
    for m in messages:
        for _, pattern, prefix in _ID_PATTERNS:
            for hit in pattern.finditer(m.content):
                if hit.group(0) not in ids.values():
                    ids[f"{prefix}{sum(1 for k in ids if k.startswith(prefix)) + 1}"] = hit.group(0)


def _resolve(value: Any, ids: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _PLACEHOLDER.sub(lambda m: ids.get(m.group(0), m.group(0)), value)
    if isinstance(value, dict):
        return {k: _resolve(v, ids) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, ids) for v in value]
    return value


def replay_trace(
    trace: Trace, conn: sqlite3.Connection, redis: Any, fallback_path: Path, policy: ModelPolicy | None = None,
) -> ReplayResult:
    """Replay recorded model outputs through the real deterministic layers. No live model is involved.

    The caller prepares ``conn`` (a migrated working copy holding the same evidence the trace was
    recorded against). Differences between recorded and fresh tool results, replies or states are
    reported as mismatches: that is how a change to the deterministic layers shows up.
    """
    from setuhaul.booking import make_hold_checker

    clock = FakeClock(parse_operational_timestamp(trace.clock_start))
    memory = ConversationMemoryStore(conn, redis, clock, load_settings(), hold_checker=make_hold_checker(conn, clock))
    sink = InMemoryEscalationSink()
    row = conn.execute("select carrier_id, phone from drivers where driver_id=?", (trace.driver_id,)).fetchone()
    trusted = TrustedConversationContext(
        driver=TrustedDriverContext(driver_id=trace.driver_id, carrier_id=row["carrier_id"], phone_e164=normalize_phone(row["phone"])),
        thread_id=trace.thread_id)
    ids: dict[str, str] = {}
    pol = policy or ModelPolicy(provider="replay", model_id="recorded")
    fresh = Trace(trace_id=trace.trace_id, source=trace.source, model=trace.model, scenario=trace.scenario, world=trace.world,
                  driver_id=trace.driver_id, thread_id=trace.thread_id, clock_start=trace.clock_start)
    result = ReplayResult(trace=fresh)

    for t in trace.turns:
        clock.advance(timedelta(seconds=t.advance_s))
        steps: list[Callable[[Sequence[ModelMessage]], ModelResponse]] = []
        for rnd in t.rounds:
            def step(msgs: Sequence[ModelMessage], rnd: RoundTrace = rnd) -> ModelResponse:
                _scan_ids(msgs, ids)
                if rnd.model_error:  # a recorded provider failure is replayed as the same kind of failure
                    kind = rnd.model_error.split(":", 1)[0]
                    raise {"TransientModelError": TransientModelError, "ModelTimeout": ModelTimeout}.get(kind, PermanentModelError)(rnd.model_error)
                return ModelResponse(text=rnd.assistant_text, tool_calls=tuple(
                    ToolCall(id=e.call_id, name=e.name, arguments=_resolve(e.arguments, ids)) for e in rnd.tool_events))
            steps.append(step)
        recorder = RecordingGateway(ScriptedGateway(steps))
        services = ConversationServices(conn=conn, memory=memory, gateway=recorder, policy=pol, sink=sink,
                                        fallback_sink=JsonlEscalationSink(fallback_path))
        before = len(sink.requests)
        reply = handle_message(InboundMessage(message_id=t.message_id, text=t.driver_text), trusted, clock, services)
        keys = list(sink.requests[-1].context) if len(sink.requests) > before else []
        fresh.turns.append(build_turn_trace(t.turn, t.message_id, t.driver_text, recorder.drain(), reply, keys, t.advance_s))

    fresh_clean = sanitize_trace(fresh)
    result.trace = fresh_clean
    for want, got in zip(trace.turns, fresh_clean.turns, strict=False):
        for name in ("reply_text", "reply_status", "escalation_reason", "verified_hold", "verified_confirmed", "loop_status"):
            if getattr(want, name) != getattr(got, name):
                result.mismatches.append(Mismatch(want.turn, name, getattr(want, name), getattr(got, name)))
        want_tools = [(e.name, e.executed, e.result) for e in _events(want)]
        got_tools = [(e.name, e.executed, e.result) for e in _events(got)]
        if want_tools != got_tools:
            result.mismatches.append(Mismatch(want.turn, "tool_events", want_tools, got_tools))
    if len(fresh_clean.turns) != len(trace.turns):
        result.mismatches.append(Mismatch(-1, "turn_count", len(trace.turns), len(fresh_clean.turns)))
    result.findings = run_evaluators(fresh_clean)
    result.violations = check_invariants(conn)
    return result
