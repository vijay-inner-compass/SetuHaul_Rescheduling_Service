"""Trace capture, sanitising, failure taxonomy, deterministic evaluators and offline replay.

A trace records one conversation: what the driver wrote, what the model asked for (tools and
arguments), what the deterministic layers answered, and what the driver was told. Evaluators are
plain functions over a trace, so they run in CI without a model. Replay feeds the *recorded model
outputs* back through the real deterministic layers and compares what they produce; it never calls
a live model.
"""

# Lazy annotations.
from __future__ import annotations

# json stores and reads traces.
import json

# re finds ids, phone numbers, emails and secrets to sanitise.
import re

# sqlite3 for the Connection type.
import sqlite3

# Callable/Sequence typing.
from collections.abc import Callable, Sequence

# dataclasses for findings and replay results.
from dataclasses import dataclass, field

# timedelta advances the replay clock between turns.
from datetime import timedelta

# Path for the fallback file.
from pathlib import Path

# Typing helpers.
from typing import Any, Literal

# Trace models are validated Pydantic models.
from pydantic import BaseModel, Field

# FakeClock drives time during replay.
from setuhaul.clock import FakeClock

# Settings give the round and tool caps checked by an evaluator.
from setuhaul.config import load_settings

# Replay runs the real conversation workflow.
from setuhaul.conversation import (
    ConversationReply,
    ConversationServices,
    InboundMessage,
    TrustedConversationContext,
    handle_message,
)

# Timestamp parsing.
from setuhaul.db import parse_operational_timestamp

# In-memory and file sinks for replayed hand-offs.
from setuhaul.escalation import InMemoryEscalationSink, JsonlEscalationSink

# Trusted driver context and phone normalisation.
from setuhaul.identity import TrustedDriverContext, normalize_phone

# Database invariants are re-checked after a replay.
from setuhaul.integrity import InvariantViolation, check_invariants

# Claim detectors used by the wording evaluator.
from setuhaul.language import claims_confirmed, claims_held

# Conversation memory for the replayed conversation.
from setuhaul.memory import ConversationMemoryStore

# Gateway and message types; ScriptedGateway replays recorded model output.
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

# Version of the trace file format.
SCHEMA_VERSION = 1
# Tools that change state.
WRITE_TOOLS = {"request_hold", "confirm_reschedule", "release_pending_change", "cancel_appointment"}
# Result statuses that mean a write really happened.
COMMITTED = {"HELD", "CONFIRMED", "RELEASED", "CANCELLED"}
# Error codes meaning a call was refused before it ran.
REJECTIONS = {"TOOL_NOT_ALLOWED", "INVALID_ARGUMENTS", "APPROVAL_NOT_PRESENT", "ONE_WRITE_PER_MESSAGE"}
# Context a hand-off must carry for a coordinator to act.
HANDOFF_KEYS = {"language", "tools_executed", "recent_turns", "selected_shipment_id"}


# ---- trace model ---------------------------------------------------------------------------------------
# One tool call as recorded: name, arguments, the result and whether it really ran.
class ToolEvent(BaseModel):
    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    executed: bool | None = None
    error: str | None = None


# One model call: the tools offered, the text returned, any provider error, and its tool events.
class RoundTrace(BaseModel):
    offered_tools: list[str] = Field(default_factory=list)
    assistant_text: str = ""
    model_error: str | None = None
    tool_events: list[ToolEvent] = Field(default_factory=list)


# One driver message: what was said, the rounds, and what the driver was told.
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


# A whole conversation.
class Trace(BaseModel):
    schema_version: int = SCHEMA_VERSION
    trace_id: str
    # 'live' traces are evidence about a real model; 'scripted' traces are only regression baselines.
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
# One raw model call, before conversion into a RoundTrace.
class RecordedCall:
    messages: tuple[ModelMessage, ...]
    offered: tuple[str, ...]
    response: ModelResponse | None
    error: str | None = None


class RecordingGateway:
    """Wraps any gateway and keeps what it saw and answered. Works for live and scripted models."""

    # Wrap another gateway.
    def __init__(self, inner: ModelGateway) -> None:
        self._inner = inner
        self.calls: list[RecordedCall] = []

    def complete(self, messages: Sequence[ModelMessage], tools: Sequence[dict[str, Any]], policy: ModelPolicy) -> ModelResponse:
        # Names of the tools offered in this call.
        offered = tuple(t["name"] for t in tools)
        try:
            # Forward to the real (or scripted) model.
            resp = self._inner.complete(messages, tools, policy)
        except Exception as exc:
            # On failure record the error text (truncated) and re-raise so behaviour is unchanged.
            self.calls.append(RecordedCall(tuple(messages), offered, None, f"{type(exc).__name__}: {str(exc)[:200]}"))
            raise
        # Record the successful call.
        self.calls.append(RecordedCall(tuple(messages), offered, resp))
        return resp

    # Return and clear the calls recorded so far (one turn's worth).
    def drain(self) -> list[RecordedCall]:
        out, self.calls = self.calls, []
        return out


# Tool results are JSON text; decode when possible.
def _parse(content: str) -> Any:
    try:
        return json.loads(content)
    except ValueError:
        return content


# Turn the recorded calls and the reply into a TurnTrace.
def build_turn_trace(
    turn: int, message_id: str, driver_text: str, calls: list[RecordedCall], reply: ConversationReply,
    handoff_context_keys: Sequence[str] = (), advance_s: int = 0,
) -> TurnTrace:
    # One RoundTrace per model call.
    rounds: list[RoundTrace] = []
    # Facts the model was given (live hold, menu).
    facts: dict[str, Any] = {}
    # Read the facts block out of the first call's system prompt.
    if calls:
        # The facts follow this marker line in the prompt.
        marker = "not by the driver)\n"
        sys_msg = calls[0].messages[0].content
        if marker in sys_msg:
            f = json.loads(sys_msg.split(marker, 1)[1])
            facts = {"live_hold": f.get("live_hold") is not None, "has_option_set": f.get("latest_option_set_id") is not None}
    # Build each round.
    for i, c in enumerate(calls):
        events: list[ToolEvent] = []
        if c.response is not None:
            # The next call's messages contain this round's tool results.
            later = calls[i + 1].messages if i + 1 < len(calls) else ()
            # Match every requested tool call with its result.
            for tcall in c.response.tool_calls:
                # Find the tool message answering this call.
                out = next((m.content for m in later if m.role == "tool" and m.tool_call_id == tcall.id), None)
                # Decode the result.
                parsed = _parse(out) if out is not None else None
                # An 'error' key means the call was refused or failed.
                err = parsed.get("error") if isinstance(parsed, dict) else None
                # executed is False for refusals, True for calls that ran, None if no result was seen.
                events.append(ToolEvent(call_id=tcall.id, name=tcall.name, arguments=dict(tcall.arguments), result=parsed,
                                        executed=None if out is None else err not in REJECTIONS, error=err))
        # Add the round.
        rounds.append(RoundTrace(offered_tools=list(c.offered), assistant_text=c.response.text if c.response else "",
                                 model_error=c.error, tool_events=events))
    # Combine into the turn record.
    return TurnTrace(turn=turn, message_id=message_id, advance_s=advance_s, driver_text=driver_text, rounds=rounds,
                     reply_text=reply.text, reply_status=reply.status, escalation_reason=reply.escalation_reason,
                     handoff_context_keys=list(handoff_context_keys), facts=facts, verified_hold=reply.verified_hold,
                     verified_confirmed=reply.verified_confirmed, loop_status=reply.loop_status)


# ---- sanitising ----------------------------------------------------------------------------------------
# Run-specific ids (menus, holds, cases) that are replaced by stable placeholders so traces compare across runs.
_ID_PATTERNS = (("OPT", re.compile(r"OPT-[0-9A-F]{12}"), "$OPT"), ("HOLD", re.compile(r"APT-H-[0-9A-F]{10}"), "$HOLD"),
                ("CASE", re.compile(r"ESC-[0-9A-F]{8}"), "$CASE"))
# Phone-number-like digit runs.
_PHONE = re.compile(r"\+?\d[\d\s-]{8,}\d")
# Email addresses.
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# API-key-like and long-hex strings.
_SECRET = re.compile(r"\b(sk-[A-Za-z0-9_-]{8,}|[A-Fa-f0-9]{40,})\b")


# Dates also look like digit runs, so they are recognised and kept.
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


# Mask a phone number but leave dates and short numbers alone.
def _redact_phone(m: re.Match[str]) -> str:
    """Digit runs that are phone numbers go; ISO dates (also digits and hyphens) stay."""
    s = m.group(0)
    # Dates and runs with fewer than 10 digits are not phone numbers.
    if _ISO_DATE.match(s.strip()) or sum(ch.isdigit() for ch in s) < 10:
        return s
    return "[PHONE]"


# Make a trace safe to store: stable ids, no personal data.
def sanitize_trace(trace: Trace) -> Trace:
    """Replace run-specific ids with stable placeholders and redact phone numbers, emails and secrets."""
    # Work on the trace as text.
    text = trace.model_dump_json()
    # For each id kind, number the distinct ids in order of appearance.
    for _, pattern, prefix in _ID_PATTERNS:
        # real id -> placeholder.
        seen: dict[str, str] = {}
        for m in pattern.finditer(text):
            seen.setdefault(m.group(0), f"{prefix}{len(seen) + 1}")
        for real, token in seen.items():
            text = text.replace(real, token)
    # Mask emails.
    text = _EMAIL.sub("[EMAIL]", text)
    # Mask secrets.
    text = _SECRET.sub("[SECRET]", text)
    # Mask phone numbers.
    text = _PHONE.sub(_redact_phone, text)
    # Re-validate the cleaned text as a Trace.
    return Trace.model_validate_json(text)


# ---- failure taxonomy ----------------------------------------------------------------------------------
# Label what went wrong in a turn.
def classify_failure(turn: TurnTrace) -> list[str]:
    """Group what went wrong in a turn: INPUT, TRAJECTORY, DECISION, DEPENDENCY, TECHNICAL_OUTCOME.
    An empty list means nothing went wrong. INSUFFICIENT_EVIDENCE means the trace cannot say."""
    # Labels found.
    out: list[str] = []
    # All tool error codes seen in the turn.
    errors = {e.error for r in turn.rounds for e in r.tool_events if e.error}
    # DEPENDENCY: a model or database failure.
    if turn.reply_status == "TEMPORARILY_UNAVAILABLE" or any(r.model_error for r in turn.rounds) or \
            turn.escalation_reason in ("MODEL_UNAVAILABLE", "DATABASE_UNAVAILABLE"):
        out.append("DEPENDENCY")
    # INPUT: the driver's language could not be handled.
    if turn.reply_status == "LANGUAGE_PROMPT" or turn.escalation_reason == "LANGUAGE_UNSUPPORTED":
        out.append("INPUT")
    # TRAJECTORY: the model tried a forbidden tool or a second write, or hit a loop cap.
    if errors & {"TOOL_NOT_ALLOWED", "ONE_WRITE_PER_MESSAGE"} or turn.loop_status in ("ROUND_CAP", "TOOL_CAP", "DEADLINE", "BUDGET"):
        out.append("TRAJECTORY")
    # DECISION: the model tried a write without approval, or used a stale or foreign id.
    if errors & {"APPROVAL_NOT_PRESENT", "NOT_LATEST_OPTION_SET", "FOREIGN_SHIPMENT", "FOREIGN_OPTION_SET", "FOREIGN_HOLD"} or \
            any(isinstance(e.result, dict) and e.result.get("status") == "NOT_LATEST_OPTION_SET" for r in turn.rounds for e in r.tool_events):
        out.append("DECISION")
    # TECHNICAL_OUTCOME: malformed tool arguments.
    if "INVALID_ARGUMENTS" in errors:
        out.append("TECHNICAL_OUTCOME")
    # INSUFFICIENT_EVIDENCE: nothing was said, so nothing can be judged.
    if not turn.reply_text.strip():
        out.append("INSUFFICIENT_EVIDENCE")
    return out


# ---- deterministic evaluators --------------------------------------------------------------------------
@dataclass(frozen=True)
# One evaluator verdict: which check, which turn, pass or fail, and why.
class Finding:
    evaluator: str
    turn: int | None
    ok: bool
    detail: str = ""


# Lower-case and collapse whitespace.
def _norm(s: str) -> str:
    return " ".join(s.casefold().split())


# All tool events across a turn's rounds.
def _events(turn: TurnTrace) -> list[ToolEvent]:
    return [e for r in turn.rounds for e in r.tool_events]


def eval_identity(trace: Trace) -> list[Finding]:
    """The model never supplies an identity: any attempt must have been rejected, never executed."""
    out = []
    # Check every tool call of every turn.
    for t in trace.turns:
        for e in _events(t):
            # A tool call carrying an identity field must have been rejected, never executed.
            if {"driver_id", "thread_id"} & set(e.arguments):
                out.append(Finding("identity", t.turn, e.executed is False and e.error == "INVALID_ARGUMENTS",
                                   f"{e.name} carried an identity field"))
    # No such calls: pass.
    return out or [Finding("identity", None, True, "no tool call carried an identity field")]


# Checks the order of actions: at most one committed write per message, a hold only after options, a confirmation only after an earlier hold.
def eval_tool_order(trace: Trace) -> list[Finding]:
    out: list[Finding] = []
    # Turn numbers when options were shown and when a hold was placed.
    searched = held_turn = None
    for t in trace.turns:
        ev = _events(t)
        # Writes that really changed state this turn.
        committed_writes = [e for e in ev if e.name in WRITE_TOOLS and e.executed and isinstance(e.result, dict) and e.result.get("status") in COMMITTED]
        # At most one.
        out.append(Finding("tool_order", t.turn, len(committed_writes) <= 1, f"{len(committed_writes)} committed writes in one message"))
        for e in ev:
            # Remember when options were shown.
            if e.executed and e.name in ("search_slot_options", "offer_original_slot") and isinstance(e.result, dict) and e.result.get("option_set_id"):
                searched = t.turn
            # A hold requires options to have been shown earlier.
            if e.executed and e.name == "request_hold" and isinstance(e.result, dict) and e.result.get("status") == "HELD":
                out.append(Finding("tool_order", t.turn, searched is not None, "hold needs options shown first"))
                held_turn = t.turn
            # A confirmation requires a hold from an EARLIER turn.
            if e.executed and e.name == "confirm_reschedule" and isinstance(e.result, dict) and e.result.get("status") == "CONFIRMED":
                out.append(Finding("tool_order", t.turn, held_turn is not None and held_turn < t.turn,
                                   "confirmation needs a hold from an earlier message"))
    return out


# Every committed write must be backed by words that appear in the driver's own message.
def eval_explicit_approval(trace: Trace) -> list[Finding]:
    out = []
    for t in trace.turns:
        for e in _events(t):
            if e.name in WRITE_TOOLS and e.executed and isinstance(e.result, dict) and e.result.get("status") in COMMITTED:
                # The quote the model gave.
                q = _norm(str(e.arguments.get("driver_quote", "")))
                out.append(Finding("explicit_approval", t.turn, len(q) >= 2 and q in _norm(t.driver_text),
                                   f"{e.name}: quote {q!r} in driver text"))
    return out or [Finding("explicit_approval", None, True, "no committed write in this trace")]


# Holds and comparisons must use a menu that this conversation was actually shown.
def eval_option_set_ownership(trace: Trace) -> list[Finding]:
    # Menus seen so far.
    out, known = [], set()
    for t in trace.turns:
        for e in _events(t):
            if isinstance(e.result, dict) and e.result.get("option_set_id") and e.name in ("search_slot_options", "offer_original_slot"):
                # Remember each menu shown.
                known.add(e.result["option_set_id"])
            if e.name in ("request_hold", "compare_option_set") and e.executed:
                # The menu id used.
                ident = e.arguments.get("option_set_id")
                # Did the system report it as foreign?
                bad = isinstance(e.result, dict) and e.result.get("error") == "FOREIGN_OPTION_SET"
                out.append(Finding("option_set_ownership", t.turn, ident in known and not bad,
                                   f"{e.name} used {ident}; known to this thread: {sorted(known)}"))
    return out or [Finding("option_set_ownership", None, True, "no option set was used")]


# Any reply that claims a hold or confirmation must be backed by committed state.
def eval_state_backed_wording(trace: Trace) -> list[Finding]:
    out = []
    for t in trace.turns:
        # Did the model read a confirmed appointment this turn?
        read_confirmed = any(e.name == "get_shipment_status" and isinstance(e.result, dict) and
                             (e.result.get("appointment") or {}).get("status") == "CONFIRMED" for e in _events(t))
        # A confirmation claim needs a verified confirmation or a read confirmed appointment.
        if claims_confirmed(t.reply_text):
            out.append(Finding("state_backed_wording", t.turn, t.verified_confirmed or read_confirmed,
                               "reply claims a confirmation"))
        # A hold claim needs a verified live hold.
        if claims_held(t.reply_text):
            out.append(Finding("state_backed_wording", t.turn, t.verified_hold or t.facts.get("live_hold", False),
                               "reply claims a hold"))
    return out or [Finding("state_backed_wording", None, True, "no booking claim in any reply")]


# Every turn must be inside the round and tool-execution limits.
def eval_round_caps(trace: Trace) -> list[Finding]:
    # The limits.
    p = load_settings()
    return [Finding("round_caps", t.turn,
                    len(t.rounds) <= p.model_max_rounds and sum(1 for e in _events(t) if e.executed) <= p.model_max_tool_calls,
                    f"{len(t.rounds)} rounds, {sum(1 for e in _events(t) if e.executed)} executed tools") for t in trace.turns]


# A hand-off must carry the context a coordinator needs.
def eval_handoff_complete(trace: Trace) -> list[Finding]:
    out = [Finding("handoff_complete", t.turn, HANDOFF_KEYS <= set(t.handoff_context_keys) or t.escalation_reason in ("LANGUAGE_UNSUPPORTED", "DATABASE_UNAVAILABLE"),
                   f"context keys {sorted(t.handoff_context_keys)}") for t in trace.turns if t.reply_status == "HANDOFF"]
    return out or [Finding("handoff_complete", None, True, "no hand-off in this trace")]


# All evaluators.
EVALUATORS: tuple[Callable[[Trace], list[Finding]], ...] = (
    eval_identity, eval_tool_order, eval_explicit_approval, eval_option_set_ownership, eval_state_backed_wording,
    eval_round_caps, eval_handoff_complete,
)


# Run them all and collect every finding.
def run_evaluators(trace: Trace) -> list[Finding]:
    return [f for ev in EVALUATORS for f in ev(trace)]


# ---- replay ---------------------------------------------------------------------------------------------
@dataclass
# A difference between recorded and replayed behaviour.
class Mismatch:
    turn: int
    field: str
    expected: Any
    actual: Any


@dataclass
# The outcome of replaying one trace.
class ReplayResult:
    trace: Trace  # what the deterministic layers produced this time (sanitised)
    mismatches: list[Mismatch] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    violations: list[InvariantViolation] = field(default_factory=list)

    @property
    # A replay is clean only if nothing mismatched, no invariant broke, and every evaluator passed.
    def ok(self) -> bool:
        return not self.mismatches and not self.violations and all(f.ok for f in self.findings)


# Matches sanitised placeholders like $OPT1.
_PLACEHOLDER = re.compile(r"\$(OPT|HOLD|CASE)(\d+)")


# Learn which real id each placeholder stands for, by scanning the messages the system produced.
def _scan_ids(messages: Sequence[ModelMessage], ids: dict[str, str]) -> None:
    for m in messages:
        for _, pattern, prefix in _ID_PATTERNS:
            for hit in pattern.finditer(m.content):
                if hit.group(0) not in ids.values():
                    ids[f"{prefix}{sum(1 for k in ids if k.startswith(prefix)) + 1}"] = hit.group(0)


# Replace placeholders in recorded tool arguments with the real ids of this replay.
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

    # Start the replay clock at the recorded time.
    clock = FakeClock(parse_operational_timestamp(trace.clock_start))
    # Fresh memory with live hold checks.
    memory = ConversationMemoryStore(conn, redis, clock, load_settings(), hold_checker=make_hold_checker(conn, clock))
    # Hand-offs from the replay are kept in memory.
    sink = InMemoryEscalationSink()
    # Look up the recorded driver to build the trusted context.
    row = conn.execute("select carrier_id, phone from drivers where driver_id=?", (trace.driver_id,)).fetchone()
    # Identity comes from the database, as in production.
    trusted = TrustedConversationContext(
        driver=TrustedDriverContext(driver_id=trace.driver_id, carrier_id=row["carrier_id"], phone_e164=normalize_phone(row["phone"])),
        thread_id=trace.thread_id)
    # Placeholder -> real id map for this replay.
    ids: dict[str, str] = {}
    # A policy that is never used for a network call.
    pol = policy or ModelPolicy(provider="replay", model_id="recorded")
    # The trace being rebuilt from the replay.
    fresh = Trace(trace_id=trace.trace_id, source=trace.source, model=trace.model, scenario=trace.scenario, world=trace.world,
                  driver_id=trace.driver_id, thread_id=trace.thread_id, clock_start=trace.clock_start)
    # The result, filled in below.
    result = ReplayResult(trace=fresh)

    # Replay each recorded turn in order.
    for t in trace.turns:
        # Advance time as it was between the recorded turns.
        clock.advance(timedelta(seconds=t.advance_s))
        # Scripted model steps rebuilt from the recorded rounds.
        steps: list[Callable[[Sequence[ModelMessage]], ModelResponse]] = []
        # One scripted step per recorded model call.
        for rnd in t.rounds:
            # Return the recorded model output (or raise the recorded provider error).
            def step(msgs: Sequence[ModelMessage], rnd: RoundTrace = rnd) -> ModelResponse:
                # Learn real ids first, so placeholders in later arguments can be resolved.
                _scan_ids(msgs, ids)
                # A recorded provider failure is replayed as the same kind of failure.
                if rnd.model_error:  # a recorded provider failure is replayed as the same kind of failure
                    kind = rnd.model_error.split(":", 1)[0]
                    raise {"TransientModelError": TransientModelError, "ModelTimeout": ModelTimeout}.get(kind, PermanentModelError)(rnd.model_error)
                # The recorded text and tool calls, with ids resolved.
                return ModelResponse(text=rnd.assistant_text, tool_calls=tuple(
                    ToolCall(id=e.call_id, name=e.name, arguments=_resolve(e.arguments, ids)) for e in rnd.tool_events))
            # Queue it.
            steps.append(step)
        # Record what the replay does, for comparison.
        recorder = RecordingGateway(ScriptedGateway(steps))
        # The real workflow with the scripted model.
        services = ConversationServices(conn=conn, memory=memory, gateway=recorder, policy=pol, sink=sink,
                                        fallback_sink=JsonlEscalationSink(fallback_path))
        # Detect new hand-offs.
        before = len(sink.requests)
        # Run the REAL deterministic layers on the recorded model output. No live model is called.
        reply = handle_message(InboundMessage(message_id=t.message_id, text=t.driver_text), trusted, clock, services)
        # Context keys of a new hand-off.
        keys = list(sink.requests[-1].context) if len(sink.requests) > before else []
        # Record what happened this time.
        fresh.turns.append(build_turn_trace(t.turn, t.message_id, t.driver_text, recorder.drain(), reply, keys, t.advance_s))

    # Sanitise so it is comparable with the stored trace.
    fresh_clean = sanitize_trace(fresh)
    # Keep the fresh trace.
    result.trace = fresh_clean
    # Compare recorded (want) and replayed (got) turn by turn.
    for want, got in zip(trace.turns, fresh_clean.turns, strict=False):
        # These fields must match exactly.
        for name in ("reply_text", "reply_status", "escalation_reason", "verified_hold", "verified_confirmed", "loop_status"):
            if getattr(want, name) != getattr(got, name):
                result.mismatches.append(Mismatch(want.turn, name, getattr(want, name), getattr(got, name)))
        # The recorded tool results.
        want_tools = [(e.name, e.executed, e.result) for e in _events(want)]
        # The replayed tool results.
        got_tools = [(e.name, e.executed, e.result) for e in _events(got)]
        # A change in tool behaviour shows up here.
        if want_tools != got_tools:
            result.mismatches.append(Mismatch(want.turn, "tool_events", want_tools, got_tools))
    # A different number of turns is also a mismatch.
    if len(fresh_clean.turns) != len(trace.turns):
        result.mismatches.append(Mismatch(-1, "turn_count", len(trace.turns), len(fresh_clean.turns)))
    # Score the replayed behaviour.
    result.findings = run_evaluators(fresh_clean)
    # Re-check database integrity.
    result.violations = check_invariants(conn)
    # The full result.
    return result
