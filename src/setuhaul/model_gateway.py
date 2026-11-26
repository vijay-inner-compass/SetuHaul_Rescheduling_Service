"""Provider-neutral model gateway and the bounded model/tool loop.

* ``ModelGateway`` is a one-method protocol. ``ScriptedGateway`` serves CI; ``AnthropicAdapter`` is
  the single provider adapter (pending operations approval, decision Q9) and is only exercised
  against a mock transport here. No secret lives in the repository: provider, model id and key come
  from the environment.
* ``run_bounded_loop`` is a LangGraph state graph (decision D13). The caps (rounds, tool executions,
  deadline, cost) are enforced by guard code in the graph's nodes, not by graph configuration, and
  the graph's own recursion limit is only a backstop.
* Model calls never happen inside a database transaction.
"""

# Lazy annotations.
from __future__ import annotations

# json encodes tool arguments for OpenAI and decodes them back.
import json

# os reads configuration (provider, model id, key) from environment variables; nothing secret lives in the repository.
import os

# Callable types scripted steps and the tool executor; Sequence is the read-only list type.
from collections.abc import Callable, Mapping, Sequence

# dataclass for the tool result.
from dataclasses import dataclass

# datetime types the loop's start time.
from datetime import datetime

# Typing helpers: Annotated attaches a merge rule to a graph state field; TypedDict describes the graph state; Protocol declares the gateway shape.
from typing import Annotated, Any, Literal, Protocol, TypedDict

# httpx is the HTTP client used by both provider adapters (tests swap in a mock transport).
import httpx

# RunnableConfig is the settings object LangGraph accepts for one run (recursion limit, callbacks).
from langchain_core.runnables import RunnableConfig

# LangGraph: StateGraph builds the model/tools loop; END marks where the loop stops.
from langgraph.graph import END, StateGraph

# Frozen validated models for messages and results.
from pydantic import BaseModel, ConfigDict, Field

# The injectable clock, used for the request deadline.
from setuhaul.clock import Clock

# Settings supply the default limits.
from setuhaul.config import load_settings


# Force every LangChain/LangSmith tracing switch off. LangSmith is installed with LangGraph and would otherwise trace silently if an environment variable were set.
def disable_external_tracing() -> None:
    """Keep model traces on this machine until operations approves a provider (Q9) and redaction.

    LangSmith ships as a transitive dependency of LangGraph and traces when these variables are set.
    """
    # Each of these variables can switch tracing on in some LangChain version.
    for var in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING"):
        # Overwrite with the string 'false' for this process.
        os.environ[var] = "false"


# ---- wire types ---------------------------------------------------------------------------------
# A request from the model to run one tool: an id (to match the reply), the tool name and its arguments.
class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


# One message in the conversation as sent to a model. role is system, user, assistant or tool.
class ModelMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    # Who the message is from.
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    # Tool calls the assistant made in this message (empty for ordinary text).
    tool_calls: tuple[ToolCall, ...] = ()
    # For a tool message: which call it answers.
    tool_call_id: str | None = None
    name: str | None = None


# What a model returned: text and/or tool calls, plus token counts used to estimate cost.
class ModelResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0


# Approved limits for model use.
class ModelPolicy(BaseModel):
    """Approved limits. Defaults are the provisional values awaiting Phase 0 approval."""

    model_config = ConfigDict(frozen=True)

    # Which provider adapter to use (anthropic or openai); empty means none configured.
    provider: str = ""
    model_id: str = ""
    # Randomness setting; 0 means as deterministic as the provider allows.
    temperature: float = 0.0
    # Longest answer allowed, which also bounds cost.
    max_output_tokens: int = 800
    # Seconds one provider call may take.
    call_timeout_s: float = 8.0
    # How many extra attempts a transient error gets.
    retries: int = 1
    # Most model calls in one driver message.
    max_rounds: int = 6
    # Most tool runs in one driver message.
    max_tool_executions: int = 4
    # Total seconds for the whole message, across all rounds.
    request_deadline_s: float = 30.0
    # Spend after which the loop stops.
    cost_budget_usd: float = 0.05
    price_in_per_mtok: float = 0.0  # supplied by operations; never guessed
    price_out_per_mtok: float = 0.0

    # Cost in USD = tokens x price per million tokens, for input and output. With prices left at 0 the estimate is 0 rather than a guess.
    def estimate_cost(self, r: ModelResponse) -> float:
        # Sum of input and output cost, scaled from per-million-token prices.
        return (r.input_tokens * self.price_in_per_mtok + r.output_tokens * self.price_out_per_mtok) / 1_000_000


# Build the policy from settings and environment variables.
def load_model_policy(env: Mapping[str, str] | None = None) -> ModelPolicy:
    # The central settings.
    s = load_settings()
    # The process environment unless the caller supplies the settings in force.
    e = os.environ if env is None else env
    return ModelPolicy(
        provider=e.get("SETUHAUL_MODEL_PROVIDER", ""),
        model_id=e.get("SETUHAUL_MODEL_ID", ""),
        max_output_tokens=s.model_max_output_tokens,
        call_timeout_s=float(s.model_timeout_seconds),
        max_rounds=s.model_max_rounds,
        max_tool_executions=s.model_max_tool_calls,
        request_deadline_s=float(s.model_request_deadline_seconds),
        cost_budget_usd=s.model_cost_budget_usd,
        price_in_per_mtok=float(e.get("SETUHAUL_MODEL_PRICE_IN_PER_MTOK", "0") or 0),
        price_out_per_mtok=float(e.get("SETUHAUL_MODEL_PRICE_OUT_PER_MTOK", "0") or 0),
    )


# ---- errors -------------------------------------------------------------------------------------
# A temporary failure: worth one retry.
class TransientModelError(Exception):
    """Worth one retry: rate limit, 5xx, connection reset."""


# A transient failure specifically caused by the time limit.
class ModelTimeout(TransientModelError):
    """The call exceeded its timeout."""


# A failure retrying cannot fix.
class PermanentModelError(Exception):
    """Do not retry: bad request, authentication, content refusal."""


# The final answer when no usable model reply could be obtained. Callers then promise nothing.
class ModelUnavailable(Exception):
    """The model could not produce a usable answer; callers must not promise anything."""

    # The code is kept separately so callers can branch on it.
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


# The one-method interface every provider (and the test double) implements.
class ModelGateway(Protocol):
    # Send one request and convert the reply.
    def complete(self, messages: Sequence[ModelMessage], tools: Sequence[dict[str, Any]], policy: ModelPolicy) -> ModelResponse: ...


# ---- test gateway -------------------------------------------------------------------------------
# A scripted step: a canned response, an exception to raise, or a function that builds a response from the messages.
Step = ModelResponse | Exception | Callable[[Sequence[ModelMessage]], ModelResponse]


# A fake model for tests: each call pops the next scripted step. It never touches the network.
class ScriptedGateway:
    """Deterministic gateway for CI. Each call consumes one scripted step and records what it saw."""

    def __init__(self, steps: Sequence[Step], on_call: Callable[[], None] | None = None) -> None:
        # Copy so the caller's list is not consumed.
        self._steps = list(steps)
        self._on_call = on_call
        # Everything the loop sent, for assertions in tests.
        self.calls: list[tuple[tuple[ModelMessage, ...], tuple[str, ...]]] = []

    def complete(self, messages: Sequence[ModelMessage], tools: Sequence[dict[str, Any]], policy: ModelPolicy) -> ModelResponse:
        self.calls.append((tuple(messages), tuple(t["name"] for t in tools)))
        # Optional hook run on every call (tests use it to advance a fake clock mid-turn).
        if self._on_call:
            self._on_call()
        # Calling the model more often than scripted is a test bug: fail loudly.
        if not self._steps:
            raise AssertionError("ScriptedGateway ran out of steps: the loop called the model more than scripted")
        # Take the next step.
        step = self._steps.pop(0)
        # An exception step simulates a provider failure.
        if isinstance(step, Exception):
            raise step
        # A function step builds its response from what the model was sent.
        if callable(step):
            return step(messages)
        return step

    @property
    # The tool names the scripted model requested in the last call's history, in order.
    def tool_sequence(self) -> list[str]:
        """Every tool name the scripted model asked for, in order."""
        out: list[str] = []
        for msgs, _ in self.calls[-1:]:
            out.extend(c.name for m in msgs if m.role == "assistant" for c in m.tool_calls)
        return out


# ---- the one provider adapter -------------------------------------------------------------------
# Build a short error text from a failed HTTP response: status plus the provider's own message. Never includes request headers or keys, and is length-capped.
def _api_error(resp: httpx.Response) -> str:
    """Status plus the provider's own error code and message (never request headers or keys), capped in length."""
    # Empty until we manage to read the provider's message.
    detail = ""
    try:
        # Both providers wrap errors in an 'error' object.
        err = resp.json().get("error", {})
        # Join the type, code and message parts that exist.
        if isinstance(err, dict):
            detail = " ".join(str(x) for x in (err.get("type"), err.get("code"), err.get("message")) if x)
        # Some providers return a plain string instead.
        elif err:
            detail = str(err)
    # The body was not JSON; report just the status.
    except ValueError:
        pass
    # Status line, with up to 200 characters of detail.
    return f"HTTP {resp.status_code}" + (f": {detail[:200]}" if detail else "")


# The providers build_gateway knows.
PROVIDERS = ("anthropic", "openai")


class UnconfiguredGateway:
    """Used when no provider is set: every turn takes the safe model-outage path."""

    # Always fails with MODEL_NOT_CONFIGURED, which sends the turn down the safe outage path.
    def complete(self, messages: Sequence[ModelMessage], tools: Sequence[dict[str, Any]], policy: ModelPolicy) -> ModelResponse:
        raise ModelUnavailable("MODEL_NOT_CONFIGURED")


# Anthropic Messages API client.
class AnthropicAdapter:
    """Messages API over httpx. ``transport`` lets tests inject a mock; the key never leaves the env."""

    # The Anthropic endpoint.
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: str | None = None, transport: httpx.BaseTransport | None = None) -> None:
        # The key comes from the argument (tests) or the environment, never from source code.
        self._key = api_key if api_key is not None else os.environ.get("SETUHAUL_MODEL_API_KEY", "")
        # An optional mock transport so tests never reach the network.
        self._transport = transport

    @staticmethod
    # Translate our neutral messages into Anthropic's format: system text is separate; tool calls and tool results are content blocks.
    def _convert(messages: Sequence[ModelMessage]) -> tuple[str, list[dict[str, Any]]]:
        # All system messages are joined into one system string.
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        out: list[dict[str, Any]] = []
        for m in messages:
            # User text passes through.
            if m.role == "user":
                out.append({"role": "user", "content": m.content})
            # Assistant text and tool calls become content blocks.
            elif m.role == "assistant":
                blocks: list[dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                # Each tool call becomes a tool_use block.
                blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in m.tool_calls]
                out.append({"role": "assistant", "content": blocks or m.content})
            # A tool result becomes a tool_result block inside a user message.
            elif m.role == "tool":
                block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
                # Consecutive tool results are merged into one user message, as the API requires.
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
        return system, out

    def complete(self, messages: Sequence[ModelMessage], tools: Sequence[dict[str, Any]], policy: ModelPolicy) -> ModelResponse:
        # No key or model id configured: report MODEL_NOT_CONFIGURED instead of calling out.
        if not self._key or not policy.model_id:
            raise ModelUnavailable("MODEL_NOT_CONFIGURED", "provider key and model id come from the environment")
        # Convert our messages to the provider's format.
        system, converted = self._convert(messages)
        # The request body.
        body: dict[str, Any] = {
            "model": policy.model_id, "max_tokens": policy.max_output_tokens, "temperature": policy.temperature,
            "system": system, "messages": converted,
            "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in tools],
        }
        # Make the HTTP call; the errors below are translated into our own error types.
        try:
            # A short-lived client with the policy's timeout.
            with httpx.Client(timeout=policy.call_timeout_s, transport=self._transport) as client:
                resp = client.post(self.URL, json=body, headers={
                    "x-api-key": self._key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        # Slow response: a transient error.
        except httpx.TimeoutException as exc:
            raise ModelTimeout(str(exc)) from exc
        # Network failure: also transient.
        except httpx.TransportError as exc:
            raise TransientModelError(str(exc)) from exc
        # Rate limit (429) or server error (5xx): transient, retry once.
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientModelError(_api_error(resp))
        # Any other 4xx (bad request, bad key): permanent, retrying will not help.
        if resp.status_code >= 400:
            raise PermanentModelError(_api_error(resp))
        # Success: decode the body.
        data = resp.json()
        # Concatenate all text blocks.
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        # Collect tool_use blocks as tool calls.
        calls = tuple(ToolCall(id=b["id"], name=b["name"], arguments=b.get("input", {}))
                      for b in data.get("content", []) if b.get("type") == "tool_use")
        # Token counts, used for the cost estimate.
        usage = data.get("usage", {})
        return ModelResponse(text=text, tool_calls=calls, input_tokens=usage.get("input_tokens", 0),
                             output_tokens=usage.get("output_tokens", 0))


# OpenAI Chat Completions client.
class OpenAIAdapter:
    """Chat Completions with function tools over httpx. ``transport`` lets tests inject a mock.

    Temperature is omitted unless SETUHAUL_OPENAI_SEND_TEMPERATURE=1, because several current OpenAI models reject any value
    other than their default. ``SETUHAUL_OPENAI_REASONING_EFFORT`` (for example ``none``) is sent as ``reasoning_effort``; some
    reasoning models refuse function tools on Chat Completions otherwise. ``SETUHAUL_OPENAI_BASE_URL`` points it at a
    compatible endpoint (for example Azure OpenAI).
    """

    def __init__(self, api_key: str | None = None, transport: httpx.BaseTransport | None = None, base_url: str | None = None,
                 reasoning_effort: str | None = None) -> None:
        # None: read the environment at each call.
        self._effort = reasoning_effort
        self._key = api_key if api_key is not None else os.environ.get("SETUHAUL_MODEL_API_KEY", "")
        self._transport = transport
        # The endpoint, overridable (for example for Azure OpenAI).
        self._url = (base_url or os.environ.get("SETUHAUL_OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"

    @staticmethod
    def _convert(messages: Sequence[ModelMessage]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role in ("system", "user"):
                out.append({"role": m.role, "content": m.content})
            elif m.role == "assistant":
                # An assistant message; content is None when it only carries tool calls.
                msg: dict[str, Any] = {"role": "assistant", "content": m.content or None}
                if m.tool_calls:
                    # Tool calls are sent as function calls with JSON-string arguments.
                    msg["tool_calls"] = [{"id": c.id, "type": "function",
                                          "function": {"name": c.name, "arguments": json.dumps(c.arguments)}} for c in m.tool_calls]
                out.append(msg)
            else:
                out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
        return out

    def complete(self, messages: Sequence[ModelMessage], tools: Sequence[dict[str, Any]], policy: ModelPolicy) -> ModelResponse:
        if not self._key or not policy.model_id:
            raise ModelUnavailable("MODEL_NOT_CONFIGURED", "provider key and model id come from the environment")
        body: dict[str, Any] = {
            "model": policy.model_id, "max_completion_tokens": policy.max_output_tokens, "messages": self._convert(messages),
            "tools": [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
                      for t in tools],
        }
        # Some providers reject an empty tools list, so omit it.
        if not tools:
            body.pop("tools")
        # Temperature is sent only when explicitly enabled, because some models reject any non-default value.
        if os.environ.get("SETUHAUL_OPENAI_SEND_TEMPERATURE") == "1":
            body["temperature"] = policy.temperature
        # Optional reasoning effort setting.
        configured = os.environ.get("SETUHAUL_OPENAI_REASONING_EFFORT", "") if self._effort is None else self._effort
        effort = configured.strip()
        if effort:  # some reasoning models reject function tools unless this is "none" (provider error, 2026-11-20)
            body["reasoning_effort"] = effort
        # Same call and the same error translation as the Anthropic adapter; only the URL, headers and body differ.
        try:
            with httpx.Client(timeout=policy.call_timeout_s, transport=self._transport) as client:
                resp = client.post(self._url, json=body, headers={"authorization": f"Bearer {self._key}", "content-type": "application/json"})
        except httpx.TimeoutException as exc:
            raise ModelTimeout(str(exc)) from exc
        except httpx.TransportError as exc:
            raise TransientModelError(str(exc)) from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientModelError(_api_error(resp))
        if resp.status_code >= 400:
            raise PermanentModelError(_api_error(resp))
        data = resp.json()
        # The first choice's message.
        choice = (data.get("choices") or [{}])[0].get("message", {})
        # Tool calls decoded from the reply.
        calls = []
        # Each requested function call.
        for c in choice.get("tool_calls") or []:
            # Arguments arrive as a JSON string.
            raw = c.get("function", {}).get("arguments", "") or "{}"
            try:
                # Decode; they must form an object.
                args = json.loads(raw)
                if not isinstance(args, dict):
                    raise TypeError("tool arguments must be a JSON object")
            # Malformed arguments are kept as a marker and rejected later by the tool layer, not crashed on.
            except (ValueError, TypeError):
                args = {"__unparseable_arguments__": raw[:200]}  # the tool layer rejects this as INVALID_ARGUMENTS
            calls.append(ToolCall(id=c.get("id", ""), name=c.get("function", {}).get("name", ""), arguments=args))
        usage = data.get("usage", {})
        return ModelResponse(text=choice.get("content") or "", tool_calls=tuple(calls),
                             input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0))


# Choose the adapter named by the policy; anything else is 'not configured'.
def build_gateway(policy: ModelPolicy, env: Mapping[str, str] | None = None,
                  transport: httpx.BaseTransport | None = None) -> ModelGateway:
    # With env given, the key and options come from it alone, never from the process environment.
    key = None if env is None else env.get("SETUHAUL_MODEL_API_KEY", "")
    if policy.provider == "anthropic":
        return AnthropicAdapter(api_key=key, transport=transport)
    if policy.provider == "openai":
        return OpenAIAdapter(api_key=key, transport=transport,
                             base_url=None if env is None else env.get("SETUHAUL_OPENAI_BASE_URL"),
                             reasoning_effort=None if env is None else env.get("SETUHAUL_OPENAI_REASONING_EFFORT", ""))
    raise ModelUnavailable("MODEL_NOT_CONFIGURED", f"unsupported or unset provider {policy.provider!r}")


# Call the model with one retry on transient errors.
def complete_with_retry(
    gateway: ModelGateway, messages: Sequence[ModelMessage], tools: Sequence[dict[str, Any]], policy: ModelPolicy
) -> ModelResponse:
    """One retry on a transient error; permanent failure or a second transient error is unavailable."""
    # Total tries = retries + 1.
    attempts = policy.retries + 1
    # Try up to that many times.
    for attempt in range(attempts):
        try:
            return gateway.complete(messages, tools, policy)
        # A temporary failure: retry, unless this was the last attempt.
        except TransientModelError as exc:
            if attempt + 1 >= attempts:
                # Distinguish a timeout from other transient failures in the error code.
                code = "MODEL_TIMEOUT" if isinstance(exc, ModelTimeout) else "MODEL_TRANSIENT_FAILURE"
                raise ModelUnavailable(code, str(exc)) from exc
        # A permanent failure is not retried.
        except PermanentModelError as exc:
            raise ModelUnavailable("MODEL_PERMANENT_FAILURE", str(exc)) from exc
        # Already the final form: pass it up.
        except ModelUnavailable:
            raise
    # Unreachable in practice; satisfies the type checker.
    raise ModelUnavailable("MODEL_TRANSIENT_FAILURE")  # pragma: no cover


# ---- the bounded loop as a LangGraph --------------------------------------------------------------
@dataclass(frozen=True)
# What running one tool returned to the loop.
class ToolResult:
    content: str
    # True only when a service actually ran. A rejected call does not count against the 4-tool cap.
    executed: bool  # True only if a service ran; rejected calls do not count against the cap
    error_code: str | None = None


# A function that runs one tool call.
ToolExecutor = Callable[[ToolCall], ToolResult]
# How the loop ended: DONE (a final answer) or a safety stop.
LoopStatus = Literal["DONE", "ROUND_CAP", "TOOL_CAP", "DEADLINE", "BUDGET", "MODEL_UNAVAILABLE"]


# The loop's result: how it ended, the final text, counters and the full message history.
class LoopOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: LoopStatus
    text: str = ""
    rounds: int = 0
    tool_executions: int = 0
    cost_usd: float = 0.0
    error_code: str | None = None
    messages: tuple[ModelMessage, ...] = ()


# How LangGraph merges new messages into state: add them to the end.
def _append(old: list[ModelMessage], new: list[ModelMessage]) -> list[ModelMessage]:
    return [*old, *new]


# The graph's shared state, passed between the two nodes.
class _State(TypedDict):
    # The conversation so far; new messages are appended.
    messages: Annotated[list[ModelMessage], _append]
    rounds: int
    tool_executions: int
    cost: float
    status: str
    text: str
    error: str


# Run the model/tool loop as a LangGraph with hard limits.
def run_bounded_loop(
    gateway: ModelGateway,
    policy: ModelPolicy,
    messages: Sequence[ModelMessage],
    tool_schemas: Sequence[dict[str, Any]],
    executor: ToolExecutor,
    clock: Clock,
    trace_env: Mapping[str, str] | None = None,
) -> LoopOutcome:
    # Make sure no tracing leaves the machine unless the opt-in tracer is explicitly built below.
    disable_external_tracing()
    # When this message began, for the deadline.
    started: datetime = clock.now()

    # Graph node 1: ask the model.
    def call_model(state: _State) -> dict[str, Any]:
        # Round cap reached: stop.
        if state["rounds"] >= policy.max_rounds:
            return {"status": "ROUND_CAP"}
        # Deadline reached: stop.
        if (clock.now() - started).total_seconds() >= policy.request_deadline_s:
            return {"status": "DEADLINE"}
        # Cost budget reached: stop.
        if state["cost"] >= policy.cost_budget_usd > 0:
            return {"status": "BUDGET"}
        try:
            # Call the model (with its one retry).
            resp = complete_with_retry(gateway, state["messages"], tool_schemas, policy)
        # No usable answer: stop with the reason.
        except ModelUnavailable as exc:
            return {"status": "MODEL_UNAVAILABLE", "error": exc.code}
        # Record this round: count it, add its cost, and append the assistant message.
        update: dict[str, Any] = {
            "rounds": state["rounds"] + 1,
            "cost": state["cost"] + policy.estimate_cost(resp),
            "messages": [ModelMessage(role="assistant", content=resp.text, tool_calls=resp.tool_calls)],
        }
        # No tool calls means the model gave its final text: finish.
        if not resp.tool_calls:
            update.update(status="DONE", text=resp.text)
        return update

    # Graph node 2: run the tool calls from the last assistant message.
    def run_tools(state: _State) -> dict[str, Any]:
        # The assistant message that requested the tools.
        last = state["messages"][-1]
        # Tool executions so far.
        executed = state["tool_executions"]
        out: list[ModelMessage] = []
        # Run each requested call in order.
        for call in last.tool_calls:
            # Tool cap reached: stop.
            if executed >= policy.max_tool_executions:
                return {"status": "TOOL_CAP", "tool_executions": executed, "messages": out}
            # Run the tool (the executor enforces identity and write rules).
            result = executor(call)
            # Only tools that really ran count toward the cap.
            executed += 1 if result.executed else 0
            # Feed the result back to the model as a tool message.
            out.append(ModelMessage(role="tool", content=result.content, tool_call_id=call.id, name=call.name))
        return {"tool_executions": executed, "messages": out}

    # Routing: a stop status ends the loop; otherwise run the tools.
    def after_model(state: _State) -> str:
        return "end" if state["status"] else "tools"

    # Routing: a stop status ends the loop; otherwise ask the model again.
    def after_tools(state: _State) -> str:
        return "end" if state["status"] else "model"

    # Assemble the graph: model <-> tools.
    graph: StateGraph[_State] = StateGraph(_State)
    graph.add_node("model", call_model)
    graph.add_node("tools", run_tools)
    # Start at the model.
    graph.set_entry_point("model")
    # After the model: go to tools or end.
    graph.add_conditional_edges("model", after_model, {"tools": "tools", "end": END})
    # After the tools: go back to the model or end.
    graph.add_conditional_edges("tools", after_tools, {"model": "model", "end": END})
    # Freeze the graph into a runnable.
    app = graph.compile()
    from setuhaul.langsmith_trace import (
        build_tracer,  # opt-in only; None unless switched on, approved and keyed
    )

    # None unless LangSmith is switched on, approved AND a key is set.
    tracer = build_tracer(trace_env)
    # Run settings. The recursion limit is only a backstop; the real caps are enforced in the nodes above.
    config: RunnableConfig = {"recursion_limit": 2 * policy.max_rounds + 10}  # backstop only
    # Attach the scrubbing tracer only when enabled.
    if tracer is not None:
        config["callbacks"] = [tracer]
        config["run_name"] = "setuhaul.conversation_loop"
    # Run the graph from its starting state.
    final: dict[str, Any] = app.invoke(
        {"messages": list(messages), "rounds": 0, "tool_executions": 0, "cost": 0.0, "status": "", "text": "", "error": ""},
        config=config,
    )
    # Package the final state. An empty status means the loop ended with a normal answer, so it is DONE.
    return LoopOutcome(
        status=final["status"] or "DONE",
        text=final["text"], rounds=final["rounds"], tool_executions=final["tool_executions"],
        cost_usd=final["cost"], error_code=final["error"] or None, messages=tuple(final["messages"]),
    )


# Describe one tool to the model: name, description and a JSON schema made from its argument model.
def tool_schema(name: str, description: str, args_model: type[BaseModel]) -> dict[str, Any]:
    return {"name": name, "description": description, "input_schema": args_model.model_json_schema()}


# Public names. 'json' is listed because tests patch it through this module.
__all__ = [
    "AnthropicAdapter",
    "LoopOutcome",
    "ModelGateway",
    "ModelMessage",
    "ModelPolicy",
    "ModelResponse",
    "ModelTimeout",
    "ModelUnavailable",
    "OpenAIAdapter",
    "PermanentModelError",
    "ScriptedGateway",
    "ToolCall",
    "ToolExecutor",
    "ToolResult",
    "TransientModelError",
    "build_gateway",
    "complete_with_retry",
    "disable_external_tracing",
    "json",
    "load_model_policy",
    "run_bounded_loop",
    "tool_schema",
]
