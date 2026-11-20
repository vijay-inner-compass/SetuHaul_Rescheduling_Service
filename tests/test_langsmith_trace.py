"""The LangSmith opt-in: off unless explicitly approved, and nothing leaves unscrubbed."""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest

from setuhaul.clock import FakeClock
from setuhaul.langsmith_trace import build_tracer, flush_tracing, redact_text, scrub, tracing_status
from setuhaul.model_gateway import (
    ModelMessage,
    ModelPolicy,
    ModelResponse,
    ScriptedGateway,
    run_bounded_loop,
)
from tests.helpers import at

ON = {"SETUHAUL_LANGSMITH": "approved", "LANGSMITH_API_KEY": "lsv2_pt_abcdefabcdefabcdefabcdef"}
POLICY = ModelPolicy(provider="scripted", model_id="s")


class StubClient:
    """Stands in for langsmith.Client: records how it was built and what the tracer asks it to send."""

    built: ClassVar[list[dict[str, Any]]] = []
    instances: ClassVar[list[StubClient]] = []

    def __init__(self, **kwargs: Any) -> None:
        StubClient.built.append(kwargs)
        StubClient.instances.append(self)
        self.kwargs = kwargs
        self.runs: list[dict[str, Any]] = []

    def create_run(self, **kw: Any) -> None:
        self.runs.append(kw)

    def update_run(self, run_id: Any, **kw: Any) -> None:
        self.runs.append({"run_id": run_id, **kw})

    def flush(self) -> None:
        pass


@pytest.fixture(autouse=True)
def reset() -> None:
    StubClient.built = []
    StubClient.instances = []


# --- off by default ------------------------------------------------------------------------------------
def test_tracing_is_off_without_the_owners_approval_even_if_a_key_exists() -> None:
    for env in ({}, {"LANGSMITH_API_KEY": "k"}, {"SETUHAUL_LANGSMITH": "yes", "LANGSMITH_API_KEY": "k"},
                {"LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": "k"}):
        s = tracing_status(env)
        assert not s.enabled and "not opted in" in s.reason
        assert build_tracer(env, client_factory=StubClient) is None
    assert StubClient.built == []


def test_approval_without_a_key_stays_off_and_says_why() -> None:
    s = tracing_status({"SETUHAUL_LANGSMITH": "approved"})
    assert not s.enabled and "LANGSMITH_API_KEY" in s.reason
    assert build_tracer({"SETUHAUL_LANGSMITH": "approved"}, client_factory=StubClient) is None


def test_approval_and_key_turn_it_on_with_defaults_and_overrides() -> None:
    s = tracing_status(ON)
    assert s.enabled and s.project == "setuhaul" and s.hide_text is False
    s2 = tracing_status({**ON, "LANGSMITH_PROJECT": "dock-pilot", "SETUHAUL_LANGSMITH_HIDE_TEXT": "1"})
    assert (s2.project, s2.hide_text) == ("dock-pilot", True)


def test_the_loop_adds_no_tracer_and_forces_global_tracing_off_when_not_approved(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for k in ("SETUHAUL_LANGSMITH", "LANGSMITH_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    seen: list[Any] = []
    import setuhaul.langsmith_trace as ls

    real = ls.build_tracer
    monkeypatch.setattr(ls, "build_tracer", lambda *a, **k: seen.append(real(*a, **k)) or seen[-1])
    out = run_bounded_loop(ScriptedGateway([ModelResponse(text="hi")]), POLICY, [ModelMessage(role="user", content="x")], [], lambda c: None, FakeClock(at("09:35")))  # type: ignore[arg-type, return-value]
    assert out.status == "DONE" and seen == [None] and os.environ["LANGSMITH_TRACING"] == "false"


# --- the client is built to scrub ----------------------------------------------------------------------
def test_the_tracer_uses_a_client_that_scrubs_inputs_outputs_and_metadata() -> None:
    t = build_tracer({**ON, "LANGSMITH_ENDPOINT": "https://eu.api.smith.langchain.com"}, client_factory=StubClient)
    assert t is not None and t.project_name == "setuhaul"
    kw = StubClient.built[0]
    assert kw["api_key"] == ON["LANGSMITH_API_KEY"] and kw["api_url"] == "https://eu.api.smith.langchain.com"
    for hook in ("hide_inputs", "hide_outputs", "hide_metadata"):
        assert "[PHONE]" in json.dumps(kw[hook]({"text": "call +91-9000010006"}))


def test_the_real_langsmith_client_is_wired_with_the_same_scrubbing(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    from langsmith import Client

    caplog.set_level(logging.CRITICAL, logger="langsmith")  # the client probes the (closed, local) endpoint and warns

    t = build_tracer({**ON, "LANGSMITH_ENDPOINT": "http://127.0.0.1:9"})
    assert t is not None and isinstance(t.client, Client)
    hidden = t.client._hide_run_inputs({"messages": [{"role": "user", "content": "mail a@b.com or +91 90000 10006"}], "api_key": "sk-abcdef123456"})
    text = json.dumps(hidden)
    assert "a@b.com" not in text and "9000010006" not in text and "sk-abcdef" not in text and "[REDACTED]" in text


def test_a_client_that_cannot_be_built_never_breaks_a_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(**kw: Any) -> None:
        raise RuntimeError("langsmith unreachable")

    assert build_tracer(ON, client_factory=broken) is None
    import langchain_core.tracers.langchain as lc

    monkeypatch.setattr(lc, "LangChainTracer", lambda **kw: (_ for _ in ()).throw(RuntimeError("tracer failed")))
    assert build_tracer(ON, client_factory=StubClient) is None


def test_when_approved_the_loop_really_traces_through_the_scrubbing_client(monkeypatch: pytest.MonkeyPatch) -> None:
    import setuhaul.langsmith_trace as ls

    real = ls.build_tracer
    monkeypatch.setattr(ls, "build_tracer", lambda *a, **k: real(ON, client_factory=StubClient))
    out = run_bounded_loop(ScriptedGateway([ModelResponse(text="hello")]), POLICY, [ModelMessage(role="user", content="x")], [],
                           lambda c: None, FakeClock(at("09:35")))  # type: ignore[arg-type, return-value]
    assert out.status == "DONE" and len(StubClient.instances) == 1
    sent = StubClient.instances[0].runs
    assert sent and any(r.get("name") == "setuhaul.conversation_loop" or r.get("name") in ("model", "tools") for r in sent)
    assert "hide_inputs" in StubClient.built[0]  # whatever the tracer sent went through a client built to scrub


# --- what the scrubber does ------------------------------------------------------------------------------
def test_scrub_masks_phones_emails_secrets_and_secret_named_keys_but_keeps_dates() -> None:
    out = scrub({"api_key": "x", "Authorization": "Bearer abc", "nested": {"note": "ring +91-9000010006 or d.r@example.com"},
                 "when": "2026-08-04T09:35:00+05:30", "key": "sk-abcdef123456", "ok": 3})
    text = json.dumps(out)
    assert out["api_key"] == "[REDACTED]" and out["Authorization"] == "[REDACTED]" and out["key"] == "[REDACTED]"
    assert "9000010006" not in text and "example.com" not in text and out["when"] == "2026-08-04T09:35:00+05:30" and out["ok"] == 3


def test_scrub_handles_model_objects_and_never_raises() -> None:
    msgs = [ModelMessage(role="user", content="my number is 9000010006 please call")]
    out = scrub({"messages": msgs, "odd": object()})
    assert "9000010006" not in json.dumps(out) and isinstance(out["odd"], str)

    class Boom:
        def model_dump(self, **k: Any) -> Any:
            raise ValueError("cannot")

    assert "withheld" in json.dumps(scrub(Boom()))


def test_hide_text_replaces_message_text_but_keeps_structure() -> None:
    out = scrub({"messages": [{"role": "user", "content": "SHP1006 is late, call 9000010006"}], "rounds": 2, "status": "DONE"}, hide_text=True)
    assert out["messages"][0]["content"] == "[hidden 32 chars]" and out["messages"][0]["role"] == "user" and out["rounds"] == 2


def test_scrub_is_idempotent() -> None:
    once = scrub({"text": "+91-9000010006 and a@b.com", "api_key": "k"})
    assert scrub(once) == once


def test_redact_text_leaves_ordinary_numbers_alone() -> None:
    assert redact_text("dock D2, 12:00-13:00, load 12000 kg, 2026-08-04") == "dock D2, 12:00-13:00, load 12000 kg, 2026-08-04"


def test_flush_is_safe_with_no_tracer_and_with_a_broken_one() -> None:
    flush_tracing(None)

    class Bad:
        def wait_for_futures(self) -> None:
            raise RuntimeError("x")

    flush_tracing(Bad())


def test_redact_text_keeps_a_date_followed_by_a_time() -> None:
    assert redact_text("eta 2026-08-04 11:50 and call +91-9000010006") == "eta 2026-08-04 11:50 and call [PHONE]"
