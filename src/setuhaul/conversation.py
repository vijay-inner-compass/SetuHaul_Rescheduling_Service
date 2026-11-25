"""Conversation workflow: one driver message in, one verified reply out.

The model interprets and drafts; code decides what is allowed and what is true. Anything that states
a fact about a booking (held, confirmed, released, cancelled, expired, hand-off) is rendered from the
state the services committed. Model text is only used for questions and explanations, and is blocked
if it claims a hold or confirmation the database does not back.
"""

# Lazy annotations.
from __future__ import annotations

# re finds clock times in model text for the output guard.
import re

# sqlite3 for the Connection type and database errors.
import sqlite3

# dataclass for the bundle of services.
from dataclasses import dataclass

# Typing helpers.
from typing import Any, Literal

# Frozen validated models.
from pydantic import BaseModel, ConfigDict, Field

# Reads facts of committed holds and confirmations.
from setuhaul.booking import confirmed_appointment_facts, get_hold

# Injectable clock.
from setuhaul.clock import Clock

# Hand-off request and sink types.
from setuhaul.escalation import EscalationSink, HandoffRequest

# The trusted driver context.
from setuhaul.identity import TrustedDriverContext

# Language detection, the fixed wording table and the claim detectors.
from setuhaul.language import (
    LANGS,
    LANGUAGE_PROMPT,
    claims_confirmed,
    claims_held,
    detect_language,
    parse_language_choice,
    say,
)

# Conversation memory.
from setuhaul.memory import ConversationContext, ConversationMemoryStore

# The bounded model loop and its types.
from setuhaul.model_gateway import (
    LoopOutcome,
    ModelGateway,
    ModelMessage,
    ModelPolicy,
    run_bounded_loop,
)

# Request ids, spans and structured logs.
from setuhaul.observability import InMemoryExporter, RequestContext, log_event, start_span

# The system prompt and the leak detector.
from setuhaul.partner_booking import render_partner_status
from setuhaul.prompts import build_system_prompt, leaks_prompt

# The tool registry and per-turn state.
from setuhaul.tools import ServiceUnavailable, ToolContext, ToolRegistry, TurnState

# How a turn ended: a normal reply, a hand-off, a temporary outage, or a request to choose a language.
ReplyStatus = Literal["REPLIED", "HANDOFF", "TEMPORARILY_UNAVAILABLE", "LANGUAGE_PROMPT"]


# One driver message: its id (for idempotency) and text.
class InboundMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: str
    # The words shown to the driver.
    text: str


class TrustedConversationContext(BaseModel):
    """Who is talking, established by the channel or session, never by the model or the message."""

    model_config = ConfigDict(frozen=True)

    # The verified driver.
    driver: TrustedDriverContext
    thread_id: str


# What the service sends back for one message.
class ConversationReply(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    # Which language it is in.
    language: str
    # How the turn ended.
    status: ReplyStatus
    # Names of tools that ran, for tests and traces.
    tools_executed: list[str] = Field(default_factory=list)
    # Why the case was handed to operations, if it was.
    escalation_reason: str | None = None
    # The hand-off case reference, if any.
    case_id: str | None = None
    # True if the database confirms the driver has a live hold.
    verified_hold: bool = False
    # True if a confirmation committed this turn.
    verified_confirmed: bool = False
    # How the model loop ended (DONE or a safety stop).
    loop_status: str | None = None
    # The request id for tracing.
    request_id: str = ""


@dataclass
# Everything one turn needs, bundled.
class ConversationServices:
    # The database connection.
    conn: sqlite3.Connection
    # Conversation memory.
    memory: ConversationMemoryStore
    # The model.
    gateway: ModelGateway
    # Model limits.
    policy: ModelPolicy
    # Where hand-offs normally go (the SQL queue).
    sink: EscalationSink
    fallback_sink: EscalationSink  # used when the primary sink (or the database) is down
    exporter: InMemoryExporter | None = None


# The single entry point: one inbound message in, one verified reply out.
def handle_message(
    message: InboundMessage, trusted: TrustedConversationContext, clock: Clock, services: ConversationServices
) -> ConversationReply:
    # A fresh request id tying all logs and spans of this turn together.
    rctx = RequestContext.new(trusted.driver.driver_id, trusted.thread_id)
    # Time the whole turn.
    with start_span("conversation.handle_message", rctx, exporter=services.exporter) as span:
        # Run the turn. Database failures and service outages are caught here and turned into a safe reply.
        try:
            # The normal path.
            reply = _handle(message, trusted, clock, services, rctx)
        # A failing database or service: reply that nothing can be promised, and record a hand-off.
        except (sqlite3.Error, ServiceUnavailable) as exc:
            # Mark the span with the reason.
            span.set_reason("DATABASE_UNAVAILABLE" if isinstance(exc, sqlite3.Error) else "SERVICE_UNAVAILABLE")
            # Build the 'cannot check bookings right now' reply.
            reply = _unavailable(message, trusted, clock, services, rctx, repr(exc))
        # Tag the span if a hand-off happened.
        if reply.escalation_reason:
            span.set_reason(reply.escalation_reason)
        # One structured log line per turn (redacted).
        log_event("conversation.reply", rctx, reason_code=reply.escalation_reason, status=reply.status,
                  tools=reply.tools_executed)
        # Attach the request id to the reply.
        return reply.model_copy(update={"request_id": rctx.request_id})


# ---- helpers ----------------------------------------------------------------------------------------
# Record a hand-off, falling back to the JSONL file if the database queue is down, so a hand-off is never lost.
def _record(services: ConversationServices, req: HandoffRequest) -> str:
    try:
        # Normal path: the SQL queue.
        return services.sink.record(req)
    except Exception:  # primary queue down (for example the database): never lose the hand-off  # noqa: BLE001 - primary escalation queue down: fall back so a hand-off is never lost
        # Fallback path.
        return services.fallback_sink.record(req)


# Build a hand-off request with the driver, thread, shipment and context filled in.
def _handoff_request(
    reason: str, trusted: TrustedConversationContext, clock: Clock, shipment_id: str | None, summary: str,
    context: dict[str, Any],
) -> HandoffRequest:
    # A request stamped with the current time.
    return HandoffRequest(reason_code=reason, driver_id=trusted.driver.driver_id, thread_id=trusted.thread_id,
                          shipment_id=shipment_id, summary=summary, context=context, created_at=clock.now())


# The database (or a service) failed mid-turn: promise nothing and record the hand-off where it survives.
def _unavailable(
    message: InboundMessage, trusted: TrustedConversationContext, clock: Clock, services: ConversationServices,
    rctx: RequestContext, detail: str,
) -> ConversationReply:
    """The database (or a service) failed mid-turn: promise nothing, record the hand-off where it survives."""
    # Reply in the driver's language if it can be told, otherwise English.
    det = detect_language(message.text)
    # Only a supported language is used.
    lang = det.language if det.language in LANGS else "en"
    # The hand-off request for the outage.
    req = _handoff_request("DATABASE_UNAVAILABLE", trusted, clock, None, "A service was unavailable while handling a driver message.",
                           {"message_id": message.message_id, "error": detail[:200], "driver_text": message.text})
    try:
        # Use the file sink directly: the database is the thing that failed.
        case = services.fallback_sink.record(req)
    # Even recording can fail; the reply is still safe.
    except Exception:  # noqa: BLE001 - recording the hand-off must not raise
        # No case reference to give.
        case = None
    # The fixed wording: nothing was booked or held.
    return ConversationReply(text=say("temp_unavailable", lang), language=str(lang), status="TEMPORARILY_UNAVAILABLE",
                             escalation_reason="DATABASE_UNAVAILABLE", case_id=case)


# System facts given to the model: the live hold, active shipments, latest menu and whether expiry was already announced.
def _facts(ctx: ConversationContext, services: ConversationServices, clock: Clock, lang: str) -> dict[str, Any]:
    # The structured state.
    state = ctx.state
    # No live hold unless the database says so.
    live = None
    # The state remembers a hold; verify it against the database.
    if state.hold is not None:
        # The hold as it is right now.
        view = get_hold(services.conn, state.hold.appointment_id, clock)
        # Only a live hold is shown to the model as a fact.
        if view is not None and view.state == "LIVE":
            live = {"appointment_id": view.appointment_id, "expires_at": view.expires_at.isoformat()}
    return {
        "reply_language": lang,
        "live_hold": live,
        "selected_shipment_id": state.selected_shipment_id,
        "active_shipment_ids": list(state.active_shipment_ids),
        "latest_option_set_id": state.latest_option_set_id,
        "system_already_told_driver_hold_expired": any(e.type == "HOLD_EXPIRED" for e in ctx.events),
        "note": "If latest_option_set_id is set and the driver is returning, call compare_option_set before re-offering it.",
    }


# Convert stored turns into model messages. Operations and warehouse messages are labelled so the model treats them as information.
def _to_model_messages(system: str, ctx: ConversationContext) -> list[ModelMessage]:
    # The system prompt first.
    out = [ModelMessage(role="system", content=system)]
    # Then the recent conversation in order.
    for t in ctx.turns:
        if t.role == "assistant":
            out.append(ModelMessage(role="assistant", content=t.text))
        elif t.role == "user":
            out.append(ModelMessage(role="user", content=t.text))
        else:
            out.append(ModelMessage(role="user", content=f"[message from operations or warehouse] {t.text}"))
    return out


# The output guard: block model text that leaks the prompt or claims a state the database does not back.
def _guard(text: str, lang: str, turn: TurnState, live_hold: bool) -> str:
    """Block model text that leaks the prompt or claims a state the database does not back."""
    # The text quotes the prompt or its canary.
    if leaks_prompt(text):
        return say("prompt_leak", lang)
    # The text claims a confirmation but none committed this turn.
    if claims_confirmed(text) and not turn.confirmed_appointment_id:
        # The model did not even read a confirmed appointment: block.
        if not turn.status_read_confirmed:
            return say("unverified_claim", lang)
        # backed only by reading an existing confirmed appointment: it may name only the times it returned
        # It read one: it may name only the times that were returned.
        mentioned = {m.replace(".", ":").zfill(5) for m in re.findall(r"\b\d{1,2}[:.]\d{2}\b", text)}
        # Any other time is invented: block.
        if mentioned - turn.status_confirmed_times:
            return say("unverified_claim", lang)
    # The text claims a hold but no live hold exists: block.
    if claims_held(text) and not live_hold:
        return say("unverified_claim", lang)
    # Nothing wrong found: the model's text passes.
    return text


# The main workflow for one message.
def _handle(
    message: InboundMessage, trusted: TrustedConversationContext, clock: Clock, services: ConversationServices,
    rctx: RequestContext,
) -> ConversationReply:
    # Short names for the identity and memory.
    driver_id, thread_id, mem = trusted.driver.driver_id, trusted.thread_id, services.memory
    # Save the driver's original text first: it is never altered.
    mem.append(driver_id, thread_id, "DRIVER", message.text, external_message_id=message.message_id)  # original text kept
    # Load recent turns and state (this also reconciles holds).
    ctx = mem.load(driver_id, thread_id)
    # The structured state.
    state = ctx.state

    # -- language -------------------------------------------------------------------------------------
    # If we asked the driver to choose a language, read their answer.
    choice = parse_language_choice(message.text) if state.language_prompts > 0 else None
    # They chose: remember it and acknowledge.
    if choice is not None:
        mem.save_structured_state(driver_id, thread_id, state.model_copy(update={"language": choice, "language_prompts": 0}))
        return _persist(services, driver_id, thread_id, ConversationReply(text=say("language_ack", choice), language=choice, status="REPLIED"))
    # Otherwise detect the language of this message.
    det = detect_language(message.text, state.language)
    # Unsupported script, or nothing to go on at all.
    if det.language == "unsupported" or (det.language == "unknown" and state.language is None):
        # Already asked once: hand off to a person rather than loop.
        if state.language_prompts >= 1:
            # A hand-off for an unsupported language.
            req = _handoff_request("LANGUAGE_UNSUPPORTED", trusted, clock, state.selected_shipment_id,
                                   "Driver writes in a language the service does not support.",
                                   {"driver_text": message.text, "detected_script": det.script})
            # Record it.
            case = _record(services, req)
            # The hand-off message in English and Hindi together.
            text = say("handoff", "en", case=case) + "\n" + say("handoff", "hi", case=case)
            return _persist(services, driver_id, thread_id, ConversationReply(
                text=text, language="unsupported", status="HANDOFF", escalation_reason="LANGUAGE_UNSUPPORTED", case_id=case))
        # First time: remember that we asked.
        mem.save_structured_state(driver_id, thread_id, state.model_copy(update={"language_prompts": 1}))
        return _persist(services, driver_id, thread_id, ConversationReply(
            text=LANGUAGE_PROMPT, language="unknown", status="LANGUAGE_PROMPT"))
    # The language to reply in.
    lang = str(det.language) if det.language in LANGS else (state.language or "en")
    # A new language was detected: remember it.
    if lang != state.language:
        mem.save_structured_state(driver_id, thread_id, state.model_copy(update={"language": lang}))
        ctx = mem.load(driver_id, thread_id)

    # -- the model loop --------------------------------------------------------------------------------
    # Per-turn tool context: identity comes from the trusted context, never from the model.
    tctx = ToolContext(conn=services.conn, clock=clock, trusted=trusted.driver, thread_id=thread_id,
                       message_id=message.message_id, message_text=message.text, memory=mem, request=rctx,
                       exporter=services.exporter)
    # The tool checkpoints for this message.
    registry = ToolRegistry(tctx)
    # The prompt (with system facts) plus conversation.
    messages = _to_model_messages(build_system_prompt(_facts(ctx, services, clock, lang)), ctx)
    # Time the model loop.
    with start_span("model.loop", rctx, exporter=services.exporter) as span:
        # Run the model with its caps: rounds, tool executions, deadline and cost.
        outcome: LoopOutcome = run_bounded_loop(services.gateway, services.policy, messages, registry.schemas(),
                                                registry.execute, clock)
        # A safety stop: mark the span.
        if outcome.status != "DONE":
            span.set_reason(outcome.error_code or outcome.status)

    # Build the verified reply.
    return _finalize(outcome, tctx, trusted, clock, services, lang, ctx)


# Turn what happened into the reply. Committed facts are worded by the system; model text is used only for questions and explanations.
def _finalize(
    outcome: LoopOutcome, tctx: ToolContext, trusted: TrustedConversationContext, clock: Clock,
    services: ConversationServices, lang: str, ctx: ConversationContext,
) -> ConversationReply:
    # What happened this turn.
    turn = tctx.turn
    # Short names.
    driver_id, thread_id, conn = trusted.driver.driver_id, trusted.thread_id, services.conn
    # The reply is built from parts.
    parts: list[str] = []
    # Hand-off reason, if any.
    reason: str | None = None
    # Hand-off case id, if any.
    case: str | None = None
    # Default status.
    status: ReplyStatus = "REPLIED"

    # facts committed this turn are worded by the system, from the committed rows
    # A confirmation committed: the wording comes from the committed appointment row.
    if turn.confirmed_appointment_id:
        parts.append(say("confirmed", lang, **confirmed_appointment_facts(conn, turn.confirmed_appointment_id)))
    # A cancellation committed.
    elif turn.cancelled_appointment_id:
        parts.append(say("cancelled", lang, ref=turn.cancelled_appointment_id))
    # A hold was placed: slot and expiry from the hold.
    elif turn.hold:
        parts.append(say("hold_placed", lang, **{k: v for k, v in turn.hold.items() if k not in ("appointment_id", "status", "note")}))
        # GPS disagreed with the driver's report when this slot was searched: say so in the system's own wording.
        if turn.gps_discrepancy_minutes is not None:
            parts.append(say("gps_note", lang, minutes=str(turn.gps_discrepancy_minutes)))
    # A hold was released.
    elif turn.released:
        parts.append(say("hold_released", lang))
    # A partner-warehouse request was made: worded from the stored request, never from the model.
    elif turn.partner_shipment_id:
        partner_text = render_partner_status(conn, turn.partner_shipment_id, lang)
        if partner_text:
            parts.append(partner_text)

    # Re-read state, as tools may have changed it.
    mem_state = services.memory.load(driver_id, thread_id).state
    # Is there a live hold now? Asked of the database.
    live_hold = False
    # Check the remembered hold.
    if mem_state.hold is not None:
        # The hold as it is right now.
        view = get_hold(conn, mem_state.hold.appointment_id, clock)
        # Only a live hold counts.
        live_hold = view is not None and view.state == "LIVE"

    # A hand-off was raised this turn: record the case and say so, in the driver's language.
    if turn.handoff is not None:
        # Add conversation context for the coordinator.
        req = turn.handoff.model_copy(update={"context": {**turn.handoff.context, **_context_tail(ctx, turn, lang)}})
        # Open (or fold into) a case.
        case = _record(services, req)
        # Return the reason and HANDOFF status.
        reason, status = req.reason_code, "HANDOFF"
        # The fixed hand-off wording with the case reference.
        parts.append(say("handoff", lang, case=case))
    elif not parts and turn.position_changed:
        # The truck moved too far away to reach the slot: said by the system, from the database result, not by the model.
        parts.append(say("position_changed", lang))
    elif not parts and turn.shown_options:
        # a menu is a stored fact too: the numbered list comes from the saved option set, never from the model's wording,
        # so "option 1" in the driver's reply always means the line the driver saw
        # How many lines the menu has.
        n = len(turn.shown_options)
        # Heading: says nothing is held.
        parts.append(say("options_intro", lang))
        # One numbered line per stored option.
        parts.extend(say("options_line", lang, n=str(i), **o) for i, o in enumerate(turn.shown_options, 1))
        # Closing line: which numbers can be chosen and until when.
        parts.append(say("options_outro", lang, choices=", ".join(str(i) for i in range(1, n + 1)), until=turn.options_valid_until))
        # GPS disagreed with the driver's report: say so in the system's own wording (only then, so other replies are unchanged).
        if turn.gps_discrepancy_minutes is not None:
            parts.append(say("gps_note", lang, minutes=str(turn.gps_discrepancy_minutes)))
    elif parts:
        pass  # something was committed: the system's wording stands alone, the model's text is not used
    # Nothing committed and no hand-off: use the model's text, but only after the output guard.
    elif outcome.status == "DONE" and outcome.text.strip():
        parts.append(_guard(outcome.text.strip(), lang, turn, live_hold))
    else:  # nothing committed and no usable model answer
        # Did the loop stop on a safety cap?
        loop_limited = outcome.status in ("ROUND_CAP", "TOOL_CAP", "DEADLINE", "BUDGET")
        # Distinguish 'ran out of rounds' from 'the model is unavailable'.
        reason = "MODEL_LOOP_LIMIT" if loop_limited else "MODEL_UNAVAILABLE"
        # A hand-off request for this failure.
        req = _handoff_request(reason, trusted, clock, ctx.state.selected_shipment_id,
                               f"Model could not complete the turn ({outcome.error_code or outcome.status}).",
                               _context_tail(ctx, turn, lang))
        case = _record(services, req)
        # A loop limit is a hand-off; an unavailable model is a temporary outage.
        status = "HANDOFF" if loop_limited else "TEMPORARILY_UNAVAILABLE"
        # The matching fixed wording.
        parts.append(say("handoff", lang, case=case) if loop_limited else say("temp_unavailable", lang))

    # If a hold lapsed while the driver was away, say so first, once.
    if any(e.type == "HOLD_EXPIRED" for e in ctx.events):
        parts.insert(0, say("hold_expired", lang))

    # Assemble the reply.
    reply = ConversationReply(
        text="\n".join(parts), language=lang, status=status, tools_executed=list(turn.executed),
        escalation_reason=reason, case_id=case, verified_hold=live_hold or bool(turn.hold),
        verified_confirmed=bool(turn.confirmed_appointment_id), loop_status=outcome.status)
    # Save the reply to the chat log and acknowledge events.
    return _persist(services, driver_id, thread_id, reply, acknowledge=bool(ctx.events))


# Context for coordinators: language, tools used, recent turns (truncated) and ids.
def _context_tail(ctx: ConversationContext, turn: TurnState, lang: str) -> dict[str, Any]:
    return {"language": lang, "tools_executed": list(turn.executed),
            "recent_turns": [{"role": t.role, "text": t.text[:400]} for t in ctx.turns[-6:]],
            "selected_shipment_id": ctx.state.selected_shipment_id,
            "latest_option_set_id": ctx.state.latest_option_set_id}


# Save the assistant's reply to the chat log.
def _persist(
    services: ConversationServices, driver_id: str, thread_id: str, reply: ConversationReply, acknowledge: bool = False
) -> ConversationReply:
    try:
        # The reply is stored as an AGENT message.
        services.memory.append(driver_id, thread_id, "AGENT", reply.text)
        # Mark expiry events as delivered.
        if acknowledge:
            services.memory.acknowledge_events(driver_id, thread_id)
    # If saving the transcript fails, the reply was already decided, so the driver still gets it.
    except sqlite3.Error:
        pass  # the reply was already decided; losing its transcript row must not change what the driver is told
    # Return the reply.
    return reply
