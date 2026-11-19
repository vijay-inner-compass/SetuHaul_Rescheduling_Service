"""Phase 8: the bounded model loop, retry policy and the single provider adapter."""

from __future__ import annotations

import json
import os
from datetime import timedelta

import httpx
import pytest

from setuhaul.clock import FakeClock
from setuhaul.model_gateway import (
    AnthropicAdapter,
    ModelMessage,
    ModelPolicy,
    ModelResponse,
    ModelTimeout,
    ModelUnavailable,
    PermanentModelError,
    ScriptedGateway,
    ToolCall,
    ToolResult,
    TransientModelError,
    complete_with_retry,
    load_model_policy,
    run_bounded_loop,
)
from tests.helpers import at

POLICY = ModelPolicy(provider="scripted", model_id="test-model")
USER = [ModelMessage(role="system", content="s"), ModelMessage(role="user", content="hi")]
TOOLS = [{"name": "ping", "description": "d", "input_schema": {"type": "object", "properties": {}}}]


def call(i: int, name: str = "ping") -> ToolCall:
    return ToolCall(id=f"c{i}", name=name, arguments={})


def tool_round(*calls: ToolCall, **tokens: int) -> ModelResponse:
    return ModelResponse(tool_calls=tuple(calls), **tokens)


def ok_executor(executed: list[str]):  # type: ignore[no-untyped-def]
    def run(c: ToolCall) -> ToolResult:
        executed.append(c.id)
        return ToolResult(content='{"ok": true}', executed=True)

    return run


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock(at("09:35"))


# --- caps ------------------------------------------------------------------------------------
def test_model_gets_at_most_six_rounds(clock: FakeClock) -> None:
    gw = ScriptedGateway([tool_round(call(i)) for i in range(20)])
    rejected = lambda c: ToolResult('{"error":"TOOL_NOT_ALLOWED"}', executed=False, error_code="TOOL_NOT_ALLOWED")
    out = run_bounded_loop(gw, POLICY, USER, TOOLS, rejected, clock)  # nothing executes, so only the round cap can stop it
    assert out.status == "ROUND_CAP" and out.rounds == 6 and len(gw.calls) == 6


def test_a_model_that_keeps_calling_working_tools_hits_the_tool_cap_first(clock: FakeClock) -> None:
    gw = ScriptedGateway([tool_round(call(i)) for i in range(20)])
    out = run_bounded_loop(gw, POLICY, USER, TOOLS, ok_executor([]), clock)
    assert out.status == "TOOL_CAP" and out.rounds == 5 and out.tool_executions == 4


def test_at_most_four_tool_executions_even_within_one_response(clock: FakeClock) -> None:
    gw = ScriptedGateway([tool_round(*(call(i) for i in range(6)))])
    ran: list[str] = []
    out = run_bounded_loop(gw, POLICY, USER, TOOLS, ok_executor(ran), clock)
    assert out.status == "TOOL_CAP" and out.tool_executions == 4 and ran == ["c0", "c1", "c2", "c3"]
    assert len(gw.calls) == 1  # the model is not asked again after the cap


def test_tool_cap_counts_across_rounds(clock: FakeClock) -> None:
    gw = ScriptedGateway([tool_round(call(1), call(2)), tool_round(call(3), call(4)), tool_round(call(5))])
    ran: list[str] = []
    out = run_bounded_loop(gw, POLICY, USER, TOOLS, ok_executor(ran), clock)
    assert out.status == "TOOL_CAP" and len(ran) == 4


def test_rejected_calls_do_not_consume_the_tool_budget(clock: FakeClock) -> None:
    gw = ScriptedGateway([tool_round(*(call(i) for i in range(5))), ModelResponse(text="done")])
    out = run_bounded_loop(gw, POLICY, USER, TOOLS,
                           lambda c: ToolResult('{"error":"INVALID_ARGUMENTS"}', executed=False, error_code="INVALID_ARGUMENTS"), clock)
    assert out.status == "DONE" and out.text == "done" and out.tool_executions == 0


def test_plain_answer_ends_the_loop_in_one_round(clock: FakeClock) -> None:
    out = run_bounded_loop(ScriptedGateway([ModelResponse(text="hello")]), POLICY, USER, TOOLS, ok_executor([]), clock)
    assert (out.status, out.text, out.rounds) == ("DONE", "hello", 1)


# --- retry, timeout, permanent failure ------------------------------------------------------------
def test_transient_error_is_retried_once(clock: FakeClock) -> None:
    gw = ScriptedGateway([TransientModelError("503"), ModelResponse(text="ok")])
    out = run_bounded_loop(gw, POLICY, USER, TOOLS, ok_executor([]), clock)
    assert out.status == "DONE" and len(gw.calls) == 2


def test_two_timeouts_make_the_model_unavailable(clock: FakeClock) -> None:
    gw = ScriptedGateway([ModelTimeout("slow"), ModelTimeout("slow")])
    out = run_bounded_loop(gw, POLICY, USER, TOOLS, ok_executor([]), clock)
    assert out.status == "MODEL_UNAVAILABLE" and out.error_code == "MODEL_TIMEOUT" and len(gw.calls) == 2


def test_permanent_failure_is_not_retried(clock: FakeClock) -> None:
    gw = ScriptedGateway([PermanentModelError("401"), ModelResponse(text="never")])
    out = run_bounded_loop(gw, POLICY, USER, TOOLS, ok_executor([]), clock)
    assert out.status == "MODEL_UNAVAILABLE" and out.error_code == "MODEL_PERMANENT_FAILURE" and len(gw.calls) == 1


def test_retry_helper_raises_model_unavailable() -> None:
    gw = ScriptedGateway([TransientModelError("a"), TransientModelError("b")])
    with pytest.raises(ModelUnavailable) as err:
        complete_with_retry(gw, USER, TOOLS, POLICY)
    assert err.value.code == "MODEL_TRANSIENT_FAILURE"


# --- deadline and cost ------------------------------------------------------------------------------
def test_request_deadline_stops_the_loop(clock: FakeClock) -> None:
    gw = ScriptedGateway([tool_round(call(1)), ModelResponse(text="late")],
                         on_call=lambda: clock.advance(timedelta(seconds=31)))
    out = run_bounded_loop(gw, POLICY, USER, TOOLS, ok_executor([]), clock)
    assert out.status == "DEADLINE" and len(gw.calls) == 1


def test_cost_budget_stops_the_loop_before_the_next_call(clock: FakeClock) -> None:
    priced = POLICY.model_copy(update={"price_in_per_mtok": 3.0, "price_out_per_mtok": 15.0, "cost_budget_usd": 0.05})
    gw = ScriptedGateway([tool_round(call(1), input_tokens=15_000, output_tokens=800), ModelResponse(text="x")])
    out = run_bounded_loop(gw, priced, USER, TOOLS, ok_executor([]), clock)
    assert out.status == "BUDGET" and out.cost_usd == pytest.approx(0.057) and len(gw.calls) == 1


def test_unpriced_policy_never_invents_a_cost(clock: FakeClock) -> None:
    gw = ScriptedGateway([tool_round(call(1), input_tokens=10**9), ModelResponse(text="fine")])
    out = run_bounded_loop(gw, POLICY, USER, TOOLS, ok_executor([]), clock)
    assert out.status == "DONE" and out.cost_usd == 0.0


# --- the loop cannot be talked into tracing off-box --------------------------------------------------
def test_external_tracing_is_forced_off(clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    run_bounded_loop(ScriptedGateway([ModelResponse(text="x")]), POLICY, USER, TOOLS, ok_executor([]), clock)
    assert os.environ["LANGSMITH_TRACING"] == "false" and os.environ["LANGCHAIN_TRACING_V2"] == "false"


def test_scripted_gateway_fails_loudly_when_over_called() -> None:
    with pytest.raises(AssertionError, match="ran out of steps"):
        ScriptedGateway([]).complete(USER, TOOLS, POLICY)


# --- the provider adapter (mock transport only) -------------------------------------------------------
def adapter(handler) -> AnthropicAdapter:  # type: ignore[no-untyped-def]
    return AnthropicAdapter(api_key="k-test", transport=httpx.MockTransport(handler))


def test_adapter_builds_the_request_and_parses_tool_use() -> None:
    seen: dict = {}  # type: ignore[type-arg]

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"], seen["headers"] = json.loads(req.content), dict(req.headers)
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "checking"}, {"type": "tool_use", "id": "tu1", "name": "ping", "input": {"a": 1}}],
            "usage": {"input_tokens": 120, "output_tokens": 30}})

    msgs = [*USER, ModelMessage(role="assistant", content="", tool_calls=(call(1),)),
            ModelMessage(role="tool", content="{}", tool_call_id="c1", name="ping")]
    resp = adapter(handler).complete(msgs, TOOLS, POLICY)
    assert resp.text == "checking" and resp.tool_calls[0].arguments == {"a": 1} and resp.input_tokens == 120
    body = seen["body"]
    assert body["model"] == "test-model" and body["temperature"] == 0.0 and body["max_tokens"] == 800
    assert body["system"] == "s" and body["tools"][0]["name"] == "ping"
    assert body["messages"][-1]["content"][0]["type"] == "tool_result"
    assert "k-test" not in json.dumps(body) and seen["headers"]["x-api-key"] == "k-test"


@pytest.mark.parametrize(("status", "exc"), [(429, TransientModelError), (503, TransientModelError), (400, PermanentModelError), (401, PermanentModelError)])
def test_adapter_classifies_http_errors(status: int, exc: type[Exception]) -> None:
    with pytest.raises(exc):
        adapter(lambda r: httpx.Response(status)).complete(USER, TOOLS, POLICY)


def test_adapter_timeout_and_connection_errors() -> None:
    def slow(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    def refused(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(ModelTimeout):
        adapter(slow).complete(USER, TOOLS, POLICY)
    with pytest.raises(TransientModelError):
        adapter(refused).complete(USER, TOOLS, POLICY)


def test_adapter_without_configuration_is_unavailable_not_a_crash() -> None:
    with pytest.raises(ModelUnavailable) as err:
        AnthropicAdapter(api_key="").complete(USER, TOOLS, POLICY)
    assert err.value.code == "MODEL_NOT_CONFIGURED"


def test_policy_comes_from_the_environment_and_holds_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETUHAUL_MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("SETUHAUL_MODEL_ID", "some-model")
    monkeypatch.setenv("SETUHAUL_MODEL_API_KEY", "secret-key")
    p = load_model_policy()
    assert (p.provider, p.model_id, p.max_rounds, p.max_tool_executions) == ("anthropic", "some-model", 6, 4)
    assert (p.call_timeout_s, p.request_deadline_s, p.max_output_tokens, p.temperature) == (8.0, 30.0, 800, 0.0)
    assert "secret-key" not in p.model_dump_json()
