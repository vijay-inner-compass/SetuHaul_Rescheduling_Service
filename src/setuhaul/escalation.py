"""Hand-off requests and where they go.

Phase 8 defines the request and two sinks. Phase 9 adds the coordinator-owned SQL queue and
lifecycle on top; ``JsonlEscalationSink`` stays as the fallback for when the database itself is
down (a hand-off that needs the database cannot be recorded in the database).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

REASON_CODES = frozenset({
    "NO_SLOT", "CONTRADICTORY_DATA", "INCOMPLETE_FEASIBILITY_DATA", "APPROVAL_REQUIRED", "SAFETY",
    "REGULATED_LOAD", "POLICY_CONFLICT", "DATABASE_UNAVAILABLE", "MODEL_UNAVAILABLE", "MODEL_LOOP_LIMIT",
    "LANGUAGE_UNSUPPORTED", "CANCELLATION_CUTOFF", "CUSTOMER_COMMITMENT", "CUSTOMER_COMMITMENT_UNKNOWN",
    "DRIVER_REQUESTED", "OTHER_UNCERTAINTY",
})


class HandoffRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    reason_code: str
    driver_id: str
    thread_id: str
    shipment_id: str | None = None
    summary: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    @field_validator("reason_code")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in REASON_CODES:
            raise ValueError(f"unknown escalation reason {v!r}")
        return v


class EscalationSink(Protocol):
    def record(self, request: HandoffRequest) -> str: ...


class InMemoryEscalationSink:
    def __init__(self) -> None:
        self.requests: list[HandoffRequest] = []

    def record(self, request: HandoffRequest) -> str:
        self.requests.append(request)
        return f"ESC-{len(self.requests):04d}"


class JsonlEscalationSink:
    """Append-only file that survives a database outage. One JSON object per line, flushed to disk."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def record(self, request: HandoffRequest) -> str:
        case_id = "ESC-" + uuid.uuid4().hex[:8].upper()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"case_id": case_id, **request.model_dump(mode="json")}, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return case_id
