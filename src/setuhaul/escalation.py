"""Hand-off requests and the coordinator-owned escalation queue.

Lifecycle: OPEN -> CLAIMED -> RESOLVED, or CANCELLED by the driver. A RESOLVED case may be reopened
by a coordinator inside a window; after that a new hand-off opens a new case. Every transition is an
immutable event. The driver is told the case was received, what happens next and that updates arrive
in the chat; a response time is mentioned only when operations has approved an SLA (Q16).

``JsonlEscalationSink`` is the fallback for when the database itself is down: a hand-off that
needs the database cannot be recorded in the database. ``ingest_fallback_file`` loads it back.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from setuhaul.clock import Clock
from setuhaul.config import load_settings
from setuhaul.db import immediate, parse_operational_timestamp
from setuhaul.identity import AuthError
from setuhaul.language import say

REASON_CODES = frozenset({
    "NO_SLOT", "CONTRADICTORY_DATA", "INCOMPLETE_FEASIBILITY_DATA", "APPROVAL_REQUIRED", "SAFETY",
    "REGULATED_LOAD", "POLICY_CONFLICT", "DATABASE_UNAVAILABLE", "MODEL_UNAVAILABLE", "MODEL_LOOP_LIMIT",
    "LANGUAGE_UNSUPPORTED", "CANCELLATION_CUTOFF", "CUSTOMER_COMMITMENT", "CUSTOMER_COMMITMENT_UNKNOWN",
    "DRIVER_REQUESTED", "OTHER_UNCERTAINTY",
})
RESOLUTION_CODES = frozenset({"ASSIGNED_SLOT", "ADVISED_DRIVER", "CANCELLED_BY_OPERATIONS", "NO_ACTION_NEEDED", "OTHER"})
Status = Literal["OPEN", "CLAIMED", "RESOLVED", "CANCELLED"]


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


# ---- the SQL queue -------------------------------------------------------------------------------------
class CaseView(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    status: Status
    reason_code: str
    driver_id: str
    thread_id: str
    shipment_id: str | None
    summary: str
    context: dict[str, Any]
    source: str
    created_at: datetime
    sla_due_at: datetime | None
    claimed_by: str | None
    claimed_at: datetime | None
    resolved_at: datetime | None
    resolution_code: str | None
    resolution_note: str | None
    reopened_count: int


class CaseCreated(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    created: bool  # False: folded into an already active case


class TransitionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["CLAIMED", "ALREADY_CLAIMED", "NOT_OPEN", "NOT_FOUND", "RESOLVED", "NOT_CLAIMED",
                    "NOT_YOUR_CASE", "CANCELLED", "ALREADY_FINISHED", "REOPENED", "REOPEN_WINDOW_PASSED",
                    "NOT_RESOLVED", "INVALID_RESOLUTION"]
    case_id: str | None = None
    claimed_by: str | None = None
    replayed: bool = False


def _view(r: sqlite3.Row) -> CaseView:
    ts = parse_operational_timestamp
    return CaseView(
        case_id=r["case_id"], status=r["status"], reason_code=r["reason_code"], driver_id=r["driver_id"],
        thread_id=r["thread_id"], shipment_id=r["shipment_id"], summary=r["summary"],
        context=json.loads(r["context_json"]), source=r["source"], created_at=ts(r["created_at"]),
        sla_due_at=ts(r["sla_due_at"]) if r["sla_due_at"] else None, claimed_by=r["claimed_by"],
        claimed_at=ts(r["claimed_at"]) if r["claimed_at"] else None,
        resolved_at=ts(r["resolved_at"]) if r["resolved_at"] else None, resolution_code=r["resolution_code"],
        resolution_note=r["resolution_note"], reopened_count=r["reopened_count"])


def _event(conn: sqlite3.Connection, case_id: str, event_type: str, actor_type: str, actor_id: str | None,
           from_status: str | None, to_status: str | None, note: str | None, now: datetime) -> None:
    conn.execute(
        "insert into escalation_events (case_id, event_type, actor_type, actor_id, from_status, to_status, note, "
        "occurred_at) values (?,?,?,?,?,?,?,?)", (case_id, event_type, actor_type, actor_id, from_status, to_status, note,
                                                  now.isoformat()))


def _set_thread(conn: sqlite3.Connection, thread_id: str, status: str) -> None:
    conn.execute("update chat_threads set thread_status=? where thread_id=?", (status, thread_id))


def _post_update(conn: sqlite3.Connection, case: sqlite3.Row, text: str, now: datetime) -> None:
    """Updates arrive in the driver's chat thread, as an OPERATIONS message."""
    conn.execute(
        "insert into chat_messages (chat_message_id, thread_id, sender_type, message_text, message_ts) "
        "values (?,?, 'OPERATIONS', ?, ?)", ("MSG-" + uuid.uuid4().hex[:12].upper(), case["thread_id"], text, now.isoformat()))


def _lang(case: sqlite3.Row) -> str:
    return str(json.loads(case["context_json"]).get("language") or "en")


def dedupe_key(req: HandoffRequest) -> str:
    raw = f"{req.driver_id}|{req.thread_id}|{req.shipment_id or ''}|{req.reason_code}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def create_handoff(conn: sqlite3.Connection, req: HandoffRequest, clock: Clock, source: str = "CONVERSATION") -> CaseCreated:
    """Open a case, or fold a duplicate request into the one already active for the same key."""
    key, now = dedupe_key(req), clock.now()
    with immediate(conn):
        existing = conn.execute("select case_id from escalation_cases where dedupe_key=? and status in ('OPEN','CLAIMED')",
                                (key,)).fetchone()
        if existing is not None:
            _event(conn, existing["case_id"], "DUPLICATE_REQUEST", "SYSTEM", None, None, None, req.summary[:200], now)
            return CaseCreated(case_id=existing["case_id"], created=False)
        case_id = "ESC-" + uuid.uuid4().hex[:8].upper()
        sla = load_settings().escalation_sla_minutes
        conn.execute(
            "insert into escalation_cases (case_id, dedupe_key, status, reason_code, driver_id, thread_id, shipment_id, "
            "summary, context_json, source, created_at, sla_due_at, updated_at) values (?,?, 'OPEN',?,?,?,?,?,?,?,?,?,?)",
            (case_id, key, req.reason_code, req.driver_id, req.thread_id, req.shipment_id, req.summary,
             json.dumps(req.context, default=str, sort_keys=True), source, req.created_at.isoformat(),
             (req.created_at + timedelta(minutes=sla)).isoformat() if sla else None, now.isoformat()))
        _event(conn, case_id, "CREATED", "SYSTEM", None, None, "OPEN", req.reason_code, now)
        _set_thread(conn, req.thread_id, "ESCALATED")
    return CaseCreated(case_id=case_id, created=True)


class SqlEscalationSink:
    """The primary sink: hand-offs become queue cases. Falls back to the JSONL sink at the call site."""

    def __init__(self, conn: sqlite3.Connection, clock: Clock) -> None:
        self._conn, self._clock = conn, clock

    def record(self, request: HandoffRequest) -> str:
        return create_handoff(self._conn, request, self._clock).case_id


def ingest_fallback_file(conn: sqlite3.Connection, path: Path, clock: Clock) -> int:
    """Load hand-offs that were written while the database was down; the file is renamed afterwards."""
    path = Path(path)
    if not path.exists():
        return 0
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        d.pop("case_id", None)
        req = HandoffRequest.model_validate(d)
        if create_handoff(conn, req, clock, source="FALLBACK_FILE").created:
            n += 1
    path.rename(path.with_suffix(path.suffix + ".ingested"))
    return n


def list_open_cases(conn: sqlite3.Connection, statuses: tuple[str, ...] = ("OPEN", "CLAIMED"), limit: int = 50) -> list[CaseView]:
    marks = ",".join("?" for _ in statuses)
    rows = conn.execute(f"select * from escalation_cases where status in ({marks}) order by created_at, case_id limit ?",
                        (*statuses, limit)).fetchall()
    return [_view(r) for r in rows]


def get_case(conn: sqlite3.Connection, case_id: str) -> CaseView | None:
    r = conn.execute("select * from escalation_cases where case_id=?", (case_id,)).fetchone()
    return _view(r) if r else None


def case_events(conn: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("select * from escalation_events where case_id=? order by event_id", (case_id,))]


def claim_case(conn: sqlite3.Connection, case_id: str, coordinator_id: str, clock: Clock) -> TransitionResult:
    now = clock.now()
    with immediate(conn):
        c = conn.execute("select * from escalation_cases where case_id=?", (case_id,)).fetchone()
        if c is None:
            return TransitionResult(status="NOT_FOUND")
        if c["status"] == "CLAIMED":
            return TransitionResult(status="ALREADY_CLAIMED", case_id=case_id, claimed_by=c["claimed_by"],
                                    replayed=c["claimed_by"] == coordinator_id)
        if c["status"] != "OPEN":
            return TransitionResult(status="NOT_OPEN", case_id=case_id)
        conn.execute("update escalation_cases set status='CLAIMED', claimed_by=?, claimed_at=?, updated_at=? "
                     "where case_id=? and status='OPEN'", (coordinator_id, now.isoformat(), now.isoformat(), case_id))
        _event(conn, case_id, "CLAIMED", "COORDINATOR", coordinator_id, "OPEN", "CLAIMED", None, now)
        _post_update(conn, c, say("case_claimed", _lang(c), case=case_id), now)
    return TransitionResult(status="CLAIMED", case_id=case_id, claimed_by=coordinator_id)


def resolve_case(conn: sqlite3.Connection, case_id: str, coordinator_id: str, resolution_code: str, note: str,
                 clock: Clock) -> TransitionResult:
    if resolution_code not in RESOLUTION_CODES:
        return TransitionResult(status="INVALID_RESOLUTION", case_id=case_id)
    now = clock.now()
    with immediate(conn):
        c = conn.execute("select * from escalation_cases where case_id=?", (case_id,)).fetchone()
        if c is None:
            return TransitionResult(status="NOT_FOUND")
        if c["status"] in ("RESOLVED", "CANCELLED"):
            return TransitionResult(status="ALREADY_FINISHED", case_id=case_id)
        if c["status"] != "CLAIMED":
            return TransitionResult(status="NOT_CLAIMED", case_id=case_id)
        if c["claimed_by"] != coordinator_id:
            return TransitionResult(status="NOT_YOUR_CASE", case_id=case_id, claimed_by=c["claimed_by"])
        conn.execute("update escalation_cases set status='RESOLVED', resolved_at=?, resolution_code=?, resolution_note=?, "
                     "updated_at=? where case_id=?", (now.isoformat(), resolution_code, note, now.isoformat(), case_id))
        _event(conn, case_id, "RESOLVED", "COORDINATOR", coordinator_id, "CLAIMED", "RESOLVED", f"{resolution_code}: {note}", now)
        _post_update(conn, c, say("case_resolved", _lang(c), case=case_id, note=note), now)
        _set_thread(conn, c["thread_id"], "OPEN")
    return TransitionResult(status="RESOLVED", case_id=case_id)


def cancel_case_by_driver(conn: sqlite3.Connection, case_id: str, driver_id: str, clock: Clock) -> TransitionResult:
    now = clock.now()
    with immediate(conn):
        c = conn.execute("select * from escalation_cases where case_id=?", (case_id,)).fetchone()
        if c is None or c["driver_id"] != driver_id:
            raise AuthError("FOREIGN_CASE")
        if c["status"] in ("RESOLVED", "CANCELLED"):
            return TransitionResult(status="ALREADY_FINISHED", case_id=case_id)
        conn.execute("update escalation_cases set status='CANCELLED', updated_at=? where case_id=?", (now.isoformat(), case_id))
        note = f"driver cancelled while claimed by {c['claimed_by']}" if c["status"] == "CLAIMED" else "driver cancelled"
        _event(conn, case_id, "CANCELLED", "DRIVER", driver_id, c["status"], "CANCELLED", note, now)
        _post_update(conn, c, say("case_cancelled", _lang(c), case=case_id), now)
        _set_thread(conn, c["thread_id"], "OPEN")
    return TransitionResult(status="CANCELLED", case_id=case_id, claimed_by=c["claimed_by"])


def reopen_case(conn: sqlite3.Connection, case_id: str, coordinator_id: str, clock: Clock) -> TransitionResult:
    """Reopen policy: only a RESOLVED case, only inside the window. Afterwards a new hand-off opens a new case."""
    now = clock.now()
    window = timedelta(hours=load_settings().escalation_reopen_window_hours)
    with immediate(conn):
        c = conn.execute("select * from escalation_cases where case_id=?", (case_id,)).fetchone()
        if c is None:
            return TransitionResult(status="NOT_FOUND")
        if c["status"] != "RESOLVED":
            return TransitionResult(status="NOT_RESOLVED", case_id=case_id)
        if now - parse_operational_timestamp(c["resolved_at"]) > window:
            return TransitionResult(status="REOPEN_WINDOW_PASSED", case_id=case_id)
        clash = conn.execute("select 1 from escalation_cases where dedupe_key=? and status in ('OPEN','CLAIMED')",
                             (c["dedupe_key"],)).fetchone()
        if clash is not None:
            return TransitionResult(status="NOT_RESOLVED", case_id=case_id)  # a newer case is already active
        conn.execute("update escalation_cases set status='OPEN', claimed_by=NULL, claimed_at=NULL, resolved_at=NULL, "
                     "resolution_code=NULL, resolution_note=NULL, reopened_count=reopened_count+1, updated_at=? "
                     "where case_id=?", (now.isoformat(), case_id))
        _event(conn, case_id, "REOPENED", "COORDINATOR", coordinator_id, "RESOLVED", "OPEN", None, now)
        _set_thread(conn, c["thread_id"], "ESCALATED")
    return TransitionResult(status="REOPENED", case_id=case_id)


class DriverCaseStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    status: Status
    text: str
    sla_minutes: int | None = None


def get_driver_case_status(conn: sqlite3.Connection, driver_id: str, thread_id: str | None = None,
                           language: str | None = None) -> DriverCaseStatus | None:
    """What the driver is told. No response time is mentioned unless an SLA is approved and set on the case."""
    sql = "select * from escalation_cases where driver_id=?" + (" and thread_id=?" if thread_id else "")
    args: tuple[str, ...] = (driver_id, thread_id) if thread_id else (driver_id,)
    rows = conn.execute(sql + " order by (status in ('OPEN','CLAIMED')) desc, created_at desc limit 1", args).fetchall()
    if not rows:
        return None
    c = rows[0]
    lang = language or _lang(c)
    note = c["resolution_note"] or ""
    text = {
        "OPEN": lambda: say("case_status_open", lang, case=c["case_id"]),
        "CLAIMED": lambda: say("case_status_claimed", lang, case=c["case_id"]),
        "RESOLVED": lambda: say("case_resolved", lang, case=c["case_id"], note=note),
        "CANCELLED": lambda: say("case_cancelled", lang, case=c["case_id"]),
    }[c["status"]]()
    sla = load_settings().escalation_sla_minutes
    if c["status"] in ("OPEN", "CLAIMED") and sla and c["sla_due_at"]:
        text += say("sla_suffix", lang, minutes=str(sla))
    return DriverCaseStatus(case_id=c["case_id"], status=c["status"], text=text,
                            sla_minutes=sla if c["sla_due_at"] else None)
