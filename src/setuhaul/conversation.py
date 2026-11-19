"""Conversation workflow: one driver message in, one verified reply out.

The model interprets and drafts; code decides what is allowed and what is true. Anything that states
a fact about a booking (held, confirmed, released, cancelled, expired, hand-off) is rendered from the
state the services committed. Model text is only used for questions and explanations, and is blocked
if it claims a hold or confirmation the database does not back.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from setuhaul.booking import confirmed_appointment_facts, get_hold
from setuhaul.clock import Clock
from setuhaul.escalation import EscalationSink, HandoffRequest
from setuhaul.identity import TrustedDriverContext
from setuhaul.language import (
    LANGS,
    LANGUAGE_PROMPT,
    claims_confirmed,
    claims_held,
    detect_language,
    parse_language_choice,
    say,
)
from setuhaul.memory import ConversationContext, ConversationMemoryStore
from setuhaul.model_gateway import (
    LoopOutcome,
    ModelGateway,
    ModelMessage,
    ModelPolicy,
    run_bounded_loop,
)
from setuhaul.observability import InMemoryExporter, RequestContext, log_event, start_span
from setuhaul.prompts import PROMPT_CANARY, build_system_prompt
from setuhaul.tools import ServiceUnavailable, ToolContext, ToolRegistry, TurnState

ReplyStatus = Literal["REPLIED", "HANDOFF", "TEMPORARILY_UNAVAILABLE", "LANGUAGE_PROMPT"]


class InboundMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: str
    text: str


class TrustedConversationContext(BaseModel):
    """Who is talking, established by the channel or session, never by the model or the message."""

    model_config = ConfigDict(frozen=True)

    driver: TrustedDriverContext
    thread_id: str


class ConversationReply(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    language: str
    status: ReplyStatus
    tools_executed: list[str] = Field(default_factory=list)
    escalation_reason: str | None = None
    case_id: str | None = None
    verified_hold: bool = False
    verified_confirmed: bool = False
    loop_status: str | None = None
    request_id: str = ""


@dataclass
class ConversationServices:
    conn: sqlite3.Connection
    memory: ConversationMemoryStore
    gateway: ModelGateway
    policy: ModelPolicy
    sink: EscalationSink
    fallback_sink: EscalationSink  # used when the primary sink (or the database) is down
    exporter: InMemoryExporter | None = None


def handle_message(
    message: InboundMessage, trusted: TrustedConversationContext, clock: Clock, services: ConversationServices
) -> ConversationReply:
    rctx = RequestContext.new(trusted.driver.driver_id, trusted.thread_id)
    with start_span("conversation.handle_message", rctx, exporter=services.exporter) as span:
        try:
            reply = _handle(message, trusted, clock, services, rctx)
        except (sqlite3.Error, ServiceUnavailable) as exc:
            span.set_reason("DATABASE_UNAVAILABLE" if isinstance(exc, sqlite3.Error) else "SERVICE_UNAVAILABLE")
            reply = _unavailable(message, trusted, clock, services, rctx, repr(exc))
        if reply.escalation_reason:
            span.set_reason(reply.escalation_reason)
        log_event("conversation.reply", rctx, reason_code=reply.escalation_reason, status=reply.status,
                  tools=reply.tools_executed)
        return reply.model_copy(update={"request_id": rctx.request_id})


# ---- helpers ----------------------------------------------------------------------------------------
def _record(services: ConversationServices, req: HandoffRequest) -> str:
    try:
        return services.sink.record(req)
    except Exception:  # primary queue down (for example the database): never lose the hand-off  # noqa: BLE001 - primary escalation queue down: fall back so a hand-off is never lost
        return services.fallback_sink.record(req)


def _handoff_request(
    reason: str, trusted: TrustedConversationContext, clock: Clock, shipment_id: str | None, summary: str,
    context: dict[str, Any],
) -> HandoffRequest:
    return HandoffRequest(reason_code=reason, driver_id=trusted.driver.driver_id, thread_id=trusted.thread_id,
                          shipment_id=shipment_id, summary=summary, context=context, created_at=clock.now())


def _unavailable(
    message: InboundMessage, trusted: TrustedConversationContext, clock: Clock, services: ConversationServices,
    rctx: RequestContext, detail: str,
) -> ConversationReply:
    """The database (or a service) failed mid-turn: promise nothing, record the hand-off where it survives."""
    det = detect_language(message.text)
    lang = det.language if det.language in LANGS else "en"
    req = _handoff_request("DATABASE_UNAVAILABLE", trusted, clock, None, "A service was unavailable while handling a driver message.",
                           {"message_id": message.message_id, "error": detail[:200], "driver_text": message.text})
    try:
        case = services.fallback_sink.record(req)
    except Exception:  # noqa: BLE001 - recording the hand-off must not raise
        case = None
    return ConversationReply(text=say("temp_unavailable", lang), language=str(lang), status="TEMPORARILY_UNAVAILABLE",
                             escalation_reason="DATABASE_UNAVAILABLE", case_id=case)


def _facts(ctx: ConversationContext, services: ConversationServices, clock: Clock, lang: str) -> dict[str, Any]:
    state = ctx.state
    live = None
    if state.hold is not None:
        view = get_hold(services.conn, state.hold.appointment_id, clock)
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


def _to_model_messages(system: str, ctx: ConversationContext) -> list[ModelMessage]:
    out = [ModelMessage(role="system", content=system)]
    for t in ctx.turns:
        if t.role == "assistant":
            out.append(ModelMessage(role="assistant", content=t.text))
        elif t.role == "user":
            out.append(ModelMessage(role="user", content=t.text))
        else:
            out.append(ModelMessage(role="user", content=f"[message from operations or warehouse] {t.text}"))
    return out


def _guard(text: str, lang: str, turn: TurnState, live_hold: bool) -> str:
    """Block model text that leaks the prompt or claims a state the database does not back."""
    if PROMPT_CANARY in text:
        return say("prompt_leak", lang)
    if claims_confirmed(text) and not turn.confirmed_appointment_id:
        if not turn.status_read_confirmed:
            return say("unverified_claim", lang)
        # backed only by reading an existing confirmed appointment: it may name only the times it returned
        mentioned = {m.replace(".", ":").zfill(5) for m in re.findall(r"\d{1,2}[:.]\d{2}", text)}
        if mentioned - turn.status_confirmed_times:
            return say("unverified_claim", lang)
    if claims_held(text) and not live_hold:
        return say("unverified_claim", lang)
    return text


def _handle(
    message: InboundMessage, trusted: TrustedConversationContext, clock: Clock, services: ConversationServices,
    rctx: RequestContext,
) -> ConversationReply:
    driver_id, thread_id, mem = trusted.driver.driver_id, trusted.thread_id, services.memory
    mem.append(driver_id, thread_id, "DRIVER", message.text, external_message_id=message.message_id)  # original text kept
    ctx = mem.load(driver_id, thread_id)
    state = ctx.state

    # -- language -------------------------------------------------------------------------------------
    choice = parse_language_choice(message.text) if state.language_prompts > 0 else None
    if choice is not None:
        mem.save_structured_state(driver_id, thread_id, state.model_copy(update={"language": choice, "language_prompts": 0}))
        return _persist(services, driver_id, thread_id, ConversationReply(text=say("language_ack", choice), language=choice, status="REPLIED"))
    det = detect_language(message.text, state.language)
    if det.language == "unsupported" or (det.language == "unknown" and state.language is None):
        if state.language_prompts >= 1:
            req = _handoff_request("LANGUAGE_UNSUPPORTED", trusted, clock, state.selected_shipment_id,
                                   "Driver writes in a language the service does not support.",
                                   {"driver_text": message.text, "detected_script": det.script})
            case = _record(services, req)
            text = say("handoff", "en", case=case) + "\n" + say("handoff", "hi", case=case)
            return _persist(services, driver_id, thread_id, ConversationReply(
                text=text, language="unsupported", status="HANDOFF", escalation_reason="LANGUAGE_UNSUPPORTED", case_id=case))
        mem.save_structured_state(driver_id, thread_id, state.model_copy(update={"language_prompts": 1}))
        return _persist(services, driver_id, thread_id, ConversationReply(
            text=LANGUAGE_PROMPT, language="unknown", status="LANGUAGE_PROMPT"))
    lang = str(det.language) if det.language in LANGS else (state.language or "en")
    if lang != state.language:
        mem.save_structured_state(driver_id, thread_id, state.model_copy(update={"language": lang}))
        ctx = mem.load(driver_id, thread_id)

    # -- the model loop --------------------------------------------------------------------------------
    tctx = ToolContext(conn=services.conn, clock=clock, trusted=trusted.driver, thread_id=thread_id,
                       message_id=message.message_id, message_text=message.text, memory=mem, request=rctx,
                       exporter=services.exporter)
    registry = ToolRegistry(tctx)
    messages = _to_model_messages(build_system_prompt(_facts(ctx, services, clock, lang)), ctx)
    with start_span("model.loop", rctx, exporter=services.exporter) as span:
        outcome: LoopOutcome = run_bounded_loop(services.gateway, services.policy, messages, registry.schemas(),
                                                registry.execute, clock)
        if outcome.status != "DONE":
            span.set_reason(outcome.error_code or outcome.status)

    return _finalize(outcome, tctx, trusted, clock, services, lang, ctx)


def _finalize(
    outcome: LoopOutcome, tctx: ToolContext, trusted: TrustedConversationContext, clock: Clock,
    services: ConversationServices, lang: str, ctx: ConversationContext,
) -> ConversationReply:
    turn = tctx.turn
    driver_id, thread_id, conn = trusted.driver.driver_id, trusted.thread_id, services.conn
    parts: list[str] = []
    reason: str | None = None
    case: str | None = None
    status: ReplyStatus = "REPLIED"

    # facts committed this turn are worded by the system, from the committed rows
    if turn.confirmed_appointment_id:
        parts.append(say("confirmed", lang, **confirmed_appointment_facts(conn, turn.confirmed_appointment_id)))
    elif turn.cancelled_appointment_id:
        parts.append(say("cancelled", lang, ref=turn.cancelled_appointment_id))
    elif turn.hold:
        parts.append(say("hold_placed", lang, **{k: v for k, v in turn.hold.items() if k != "appointment_id"}))
    elif turn.released:
        parts.append(say("hold_released", lang))

    mem_state = services.memory.load(driver_id, thread_id).state
    live_hold = False
    if mem_state.hold is not None:
        view = get_hold(conn, mem_state.hold.appointment_id, clock)
        live_hold = view is not None and view.state == "LIVE"

    if turn.handoff is not None:
        req = turn.handoff.model_copy(update={"context": {**turn.handoff.context, **_context_tail(ctx, turn, lang)}})
        case = _record(services, req)
        reason, status = req.reason_code, "HANDOFF"
        parts.append(say("handoff", lang, case=case))
    elif parts:
        pass  # something was committed: the system's wording stands alone, the model's text is not used
    elif outcome.status == "DONE" and outcome.text.strip():
        parts.append(_guard(outcome.text.strip(), lang, turn, live_hold))
    else:  # nothing committed and no usable model answer
        loop_limited = outcome.status in ("ROUND_CAP", "TOOL_CAP", "DEADLINE", "BUDGET")
        reason = "MODEL_LOOP_LIMIT" if loop_limited else "MODEL_UNAVAILABLE"
        req = _handoff_request(reason, trusted, clock, ctx.state.selected_shipment_id,
                               f"Model could not complete the turn ({outcome.error_code or outcome.status}).",
                               _context_tail(ctx, turn, lang))
        case = _record(services, req)
        status = "HANDOFF" if loop_limited else "TEMPORARILY_UNAVAILABLE"
        parts.append(say("handoff", lang, case=case) if loop_limited else say("temp_unavailable", lang))

    if any(e.type == "HOLD_EXPIRED" for e in ctx.events):
        parts.insert(0, say("hold_expired", lang))

    reply = ConversationReply(
        text="\n".join(parts), language=lang, status=status, tools_executed=list(turn.executed),
        escalation_reason=reason, case_id=case, verified_hold=live_hold or bool(turn.hold),
        verified_confirmed=bool(turn.confirmed_appointment_id), loop_status=outcome.status)
    return _persist(services, driver_id, thread_id, reply, acknowledge=bool(ctx.events))


def _context_tail(ctx: ConversationContext, turn: TurnState, lang: str) -> dict[str, Any]:
    return {"language": lang, "tools_executed": list(turn.executed),
            "recent_turns": [{"role": t.role, "text": t.text[:400]} for t in ctx.turns[-6:]],
            "selected_shipment_id": ctx.state.selected_shipment_id,
            "latest_option_set_id": ctx.state.latest_option_set_id}


def _persist(
    services: ConversationServices, driver_id: str, thread_id: str, reply: ConversationReply, acknowledge: bool = False
) -> ConversationReply:
    try:
        services.memory.append(driver_id, thread_id, "AGENT", reply.text)
        if acknowledge:
            services.memory.acknowledge_events(driver_id, thread_id)
    except sqlite3.Error:
        pass  # the reply was already decided; losing its transcript row must not change what the driver is told
    return reply
