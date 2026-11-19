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

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal, Protocol, TypedDict

import httpx
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, ConfigDict, Field

from setuhaul.clock import Clock
from setuhaul.config import load_settings


def disable_external_tracing() -> None:
    """Keep model traces on this machine until operations approves a provider (Q9) and redaction.

    LangSmith ships as a transitive dependency of LangGraph and traces when these variables are set.
    """
    for var in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING"):
        os.environ[var] = "false"


# ---- wire types ---------------------------------------------------------------------------------
class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ModelMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None


class ModelResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0


class ModelPolicy(BaseModel):
    """Approved limits. Defaults are the provisional values awaiting Phase 0 approval."""

    model_config = ConfigDict(frozen=True)

    provider: str = ""
    model_id: str = ""
    temperature: float = 0.0
    max_output_tokens: int = 800
    call_timeout_s: float = 8.0
    retries: int = 1
    max_rounds: int = 6
    max_tool_executions: int = 4
    request_deadline_s: float = 30.0
    cost_budget_usd: float = 0.05
    price_in_per_mtok: float = 0.0  # supplied by operations; never guessed
    price_out_per_mtok: float = 0.0

    def estimate_cost(self, r: ModelResponse) -> float:
        return (r.input_tokens * self.price_in_per_mtok + r.output_tokens * self.price_out_per_mtok) / 1_000_000


def load_model_policy() -> ModelPolicy:
    s = load_settings()
    return ModelPolicy(
        provider=os.environ.get("SETUHAUL_MODEL_PROVIDER", ""),
        model_id=os.environ.get("SETUHAUL_MODEL_ID", ""),
        max_output_tokens=s.model_max_output_tokens,
        call_timeout_s=float(s.model_timeout_seconds),
        max_rounds=s.model_max_rounds,
        max_tool_executions=s.model_max_tool_calls,
        request_deadline_s=float(s.model_request_deadline_seconds),
        cost_budget_usd=s.model_cost_budget_usd,
        price_in_per_mtok=float(os.environ.get("SETUHAUL_MODEL_PRICE_IN_PER_MTOK", "0") or 0),
        price_out_per_mtok=float(os.environ.get("SETUHAUL_MODEL_PRICE_OUT_PER_MTOK", "0") or 0),
    )


# ---- errors -------------------------------------------------------------------------------------
class TransientModelError(Exception):
    """Worth one retry: rate limit, 5xx, connection reset."""


class ModelTimeout(TransientModelError):
    """The call exceeded its timeout."""


class PermanentModelError(Exception):
    """Do not retry: bad request, authentication, content refusal."""


class ModelUnavailable(Exception):
    """The model could not produce a usable answer; callers must not promise anything."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class ModelGateway(Protocol):
    def complete(self, messages: Sequence[ModelMessage], tools: Sequence[dict[str, Any]], policy: ModelPolicy) -> ModelResponse: ...


# ---- test gateway -------------------------------------------------------------------------------
Step = ModelResponse | Exception | Callable[[Sequence[ModelMessage]], ModelResponse]


class ScriptedGateway:
    """Deterministic gateway for CI. Each call consumes one scripted step and records what it saw."""

    def __init__(self, steps: Sequence[Step], on_call: Callable[[], None] | None = None) -> None:
        self._steps = list(steps)
        self._on_call = on_call
        self.calls: list[tuple[tuple[ModelMessage, ...], tuple[str, ...]]] = []

    def complete(self, messages: Sequence[ModelMessage], tools: Sequence[dict[str, Any]], policy: ModelPolicy) -> ModelResponse:
        self.calls.append((tuple(messages), tuple(t["name"] for t in tools)))
        if self._on_call:
            self._on_call()
        if not self._steps:
            raise AssertionError("ScriptedGateway ran out of steps: the loop called the model more than scripted")
        step = self._steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step(messages)
        return step

    @property
    def tool_sequence(self) -> list[str]:
        """Every tool name the scripted model asked for, in order."""
        out: list[str] = []
        for msgs, _ in self.calls[-1:]:
            out.extend(c.name for m in msgs if m.role == "assistant" for c in m.tool_calls)
        return out


# ---- the one provider adapter -------------------------------------------------------------------
class AnthropicAdapter:
    """Messages API over httpx. ``transport`` lets tests inject a mock; the key never leaves the env."""

    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: str | None = None, transport: httpx.BaseTransport | None = None) -> None:
        self._key = api_key if api_key is not None else os.environ.get("SETUHAUL_MODEL_API_KEY", "")
        self._transport = transport

    @staticmethod
    def _convert(messages: Sequence[ModelMessage]) -> tuple[str, list[dict[str, Any]]]:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "user":
                out.append({"role": "user", "content": m.content})
            elif m.role == "assistant":
                blocks: list[dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in m.tool_calls]
                out.append({"role": "assistant", "content": blocks or m.content})
            elif m.role == "tool":
                block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
        return system, out

    def complete(self, messages: Sequence[ModelMessage], tools: Sequence[dict[str, Any]], policy: ModelPolicy) -> ModelResponse:
        if not self._key or not policy.model_id:
            raise ModelUnavailable("MODEL_NOT_CONFIGURED", "provider key and model id come from the environment")
        system, converted = self._convert(messages)
        body: dict[str, Any] = {
            "model": policy.model_id, "max_tokens": policy.max_output_tokens, "temperature": policy.temperature,
            "system": system, "messages": converted,
            "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in tools],
        }
        try:
            with httpx.Client(timeout=policy.call_timeout_s, transport=self._transport) as client:
                resp = client.post(self.URL, json=body, headers={
                    "x-api-key": self._key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        except httpx.TimeoutException as exc:
            raise ModelTimeout(str(exc)) from exc
        except httpx.TransportError as exc:
            raise TransientModelError(str(exc)) from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientModelError(f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise PermanentModelError(f"HTTP {resp.status_code}")
        data = resp.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        calls = tuple(ToolCall(id=b["id"], name=b["name"], arguments=b.get("input", {}))
                      for b in data.get("content", []) if b.get("type") == "tool_use")
        usage = data.get("usage", {})
        return ModelResponse(text=text, tool_calls=calls, input_tokens=usage.get("input_tokens", 0),
                             output_tokens=usage.get("output_tokens", 0))


def build_gateway(policy: ModelPolicy) -> ModelGateway:
    if policy.provider == "anthropic":
        return AnthropicAdapter()
    raise ModelUnavailable("MODEL_NOT_CONFIGURED", f"unsupported or unset provider {policy.provider!r}")


def complete_with_retry(
    gateway: ModelGateway, messages: Sequence[ModelMessage], tools: Sequence[dict[str, Any]], policy: ModelPolicy
) -> ModelResponse:
    """One retry on a transient error; permanent failure or a second transient error is unavailable."""
    attempts = policy.retries + 1
    for attempt in range(attempts):
        try:
            return gateway.complete(messages, tools, policy)
        except TransientModelError as exc:
            if attempt + 1 >= attempts:
                code = "MODEL_TIMEOUT" if isinstance(exc, ModelTimeout) else "MODEL_TRANSIENT_FAILURE"
                raise ModelUnavailable(code, str(exc)) from exc
        except PermanentModelError as exc:
            raise ModelUnavailable("MODEL_PERMANENT_FAILURE", str(exc)) from exc
        except ModelUnavailable:
            raise
    raise ModelUnavailable("MODEL_TRANSIENT_FAILURE")  # pragma: no cover


# ---- the bounded loop as a LangGraph --------------------------------------------------------------
@dataclass(frozen=True)
class ToolResult:
    content: str
    executed: bool  # True only if a service ran; rejected calls do not count against the cap
    error_code: str | None = None


ToolExecutor = Callable[[ToolCall], ToolResult]
LoopStatus = Literal["DONE", "ROUND_CAP", "TOOL_CAP", "DEADLINE", "BUDGET", "MODEL_UNAVAILABLE"]


class LoopOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: LoopStatus
    text: str = ""
    rounds: int = 0
    tool_executions: int = 0
    cost_usd: float = 0.0
    error_code: str | None = None
    messages: tuple[ModelMessage, ...] = ()


def _append(old: list[ModelMessage], new: list[ModelMessage]) -> list[ModelMessage]:
    return [*old, *new]


class _State(TypedDict):
    messages: Annotated[list[ModelMessage], _append]
    rounds: int
    tool_executions: int
    cost: float
    status: str
    text: str
    error: str


def run_bounded_loop(
    gateway: ModelGateway,
    policy: ModelPolicy,
    messages: Sequence[ModelMessage],
    tool_schemas: Sequence[dict[str, Any]],
    executor: ToolExecutor,
    clock: Clock,
) -> LoopOutcome:
    disable_external_tracing()
    started: datetime = clock.now()

    def call_model(state: _State) -> dict[str, Any]:
        if state["rounds"] >= policy.max_rounds:
            return {"status": "ROUND_CAP"}
        if (clock.now() - started).total_seconds() >= policy.request_deadline_s:
            return {"status": "DEADLINE"}
        if state["cost"] >= policy.cost_budget_usd > 0:
            return {"status": "BUDGET"}
        try:
            resp = complete_with_retry(gateway, state["messages"], tool_schemas, policy)
        except ModelUnavailable as exc:
            return {"status": "MODEL_UNAVAILABLE", "error": exc.code}
        update: dict[str, Any] = {
            "rounds": state["rounds"] + 1,
            "cost": state["cost"] + policy.estimate_cost(resp),
            "messages": [ModelMessage(role="assistant", content=resp.text, tool_calls=resp.tool_calls)],
        }
        if not resp.tool_calls:
            update.update(status="DONE", text=resp.text)
        return update

    def run_tools(state: _State) -> dict[str, Any]:
        last = state["messages"][-1]
        executed = state["tool_executions"]
        out: list[ModelMessage] = []
        for call in last.tool_calls:
            if executed >= policy.max_tool_executions:
                return {"status": "TOOL_CAP", "tool_executions": executed, "messages": out}
            result = executor(call)
            executed += 1 if result.executed else 0
            out.append(ModelMessage(role="tool", content=result.content, tool_call_id=call.id, name=call.name))
        return {"tool_executions": executed, "messages": out}

    def after_model(state: _State) -> str:
        return "end" if state["status"] else "tools"

    def after_tools(state: _State) -> str:
        return "end" if state["status"] else "model"

    graph: StateGraph[_State] = StateGraph(_State)
    graph.add_node("model", call_model)
    graph.add_node("tools", run_tools)
    graph.set_entry_point("model")
    graph.add_conditional_edges("model", after_model, {"tools": "tools", "end": END})
    graph.add_conditional_edges("tools", after_tools, {"model": "model", "end": END})
    app = graph.compile()
    final: dict[str, Any] = app.invoke(
        {"messages": list(messages), "rounds": 0, "tool_executions": 0, "cost": 0.0, "status": "", "text": "", "error": ""},
        config={"recursion_limit": 2 * policy.max_rounds + 10},  # backstop only
    )
    return LoopOutcome(
        status=final["status"] or "DONE",
        text=final["text"], rounds=final["rounds"], tool_executions=final["tool_executions"],
        cost_usd=final["cost"], error_code=final["error"] or None, messages=tuple(final["messages"]),
    )


def tool_schema(name: str, description: str, args_model: type[BaseModel]) -> dict[str, Any]:
    return {"name": name, "description": description, "input_schema": args_model.model_json_schema()}


__all__ = [
    "AnthropicAdapter", "LoopOutcome", "ModelGateway", "ModelMessage", "ModelPolicy", "ModelResponse",
    "ModelTimeout", "ModelUnavailable", "PermanentModelError", "ScriptedGateway", "ToolCall", "ToolExecutor",
    "ToolResult", "TransientModelError", "build_gateway", "complete_with_retry", "disable_external_tracing",
    "json", "load_model_policy", "run_bounded_loop", "tool_schema",
]
