"""Phase 1: request IDs, spans, redaction, stable reason codes."""

from __future__ import annotations

import json
import logging

from setuhaul.observability import (
    InMemoryExporter,
    RequestContext,
    log_event,
    redact,
    start_span,
)


def test_request_id_generated_and_propagated_to_child_span() -> None:
    exp = InMemoryExporter()
    ctx = RequestContext.new(driver_id="DRV1", thread_id="T1")
    with start_span("outer", ctx, exporter=exp) as outer, start_span("inner", ctx, parent=outer, exporter=exp):
        pass
    names = {s.name: s for s in exp.spans}
    assert names["inner"].parent_id == names["outer"].span_id
    assert names["inner"].request_id == names["outer"].request_id == ctx.request_id
    assert names["outer"].parent_id is None


def test_span_records_error_and_reason_code() -> None:
    exp = InMemoryExporter()
    ctx = RequestContext.new()
    try:
        with start_span("boom", ctx, exporter=exp) as s:
            s.set_reason("LOCK_TIMEOUT")
            raise RuntimeError("x")
    except RuntimeError:
        pass
    assert exp.spans[0].status == "ERROR"
    assert exp.spans[0].reason_code == "LOCK_TIMEOUT"


def test_redact_masks_secrets_and_phones() -> None:
    out = redact({"api_key": "sk-secret", "phone": "+919876543210", "note": "call +919876543210",
                  "nested": {"Authorization": "Bearer abc", "ok": 1}})
    blob = json.dumps(out)
    assert "sk-secret" not in blob and "919876543210" not in blob and "Bearer abc" not in blob
    assert out["nested"]["ok"] == 1


def test_log_event_is_structured_redacted_with_request_id(caplog) -> None:
    ctx = RequestContext.new(driver_id="DRV1", thread_id="T1")
    with caplog.at_level(logging.INFO, logger="setuhaul"):
        log_event("hold.rejected", ctx, reason_code="SLOT_CONFLICT", token="abc123")
    rec = json.loads(caplog.records[-1].getMessage())
    assert rec["request_id"] == ctx.request_id
    assert rec["reason_code"] == "SLOT_CONFLICT"
    assert rec["token"] != "abc123"
