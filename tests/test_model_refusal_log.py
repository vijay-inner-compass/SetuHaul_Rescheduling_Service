"""A call the provider rejects leaves its reason in the server log, without keys."""

from __future__ import annotations

import logging

import pytest

from setuhaul.model_gateway import (
    ModelPolicy,
    ModelResponse,
    ModelUnavailable,
    PermanentModelError,
    complete_with_retry,
)


class _Refuses:
    def complete(self, messages: object, tools: object, policy: object) -> ModelResponse:
        raise PermanentModelError("HTTP 400: tools not supported; Incorrect key sk-proj-abc***XYZ and Bearer sk-live_123")


def test_the_providers_reason_is_logged_without_keys(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    with pytest.raises(ModelUnavailable, match="MODEL_PERMANENT_FAILURE"):
        complete_with_retry(_Refuses(), [], [], ModelPolicy(provider="openai", model_id="m1"))
    assert "HTTP 400: tools not supported" in caplog.text and "openai / m1" in caplog.text
    assert "sk-proj" not in caplog.text and "sk-live" not in caplog.text
