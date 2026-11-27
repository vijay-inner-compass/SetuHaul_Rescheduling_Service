"""Hand-off requests and the coordinator-owned escalation queue.

Lifecycle: OPEN -> CLAIMED -> RESOLVED, or CANCELLED by the driver. A RESOLVED case may be reopened
by a coordinator inside a window; after that a new hand-off opens a new case. Every transition is an
immutable event. The driver is told the case was received, what happens next and that updates arrive
in the chat; a response time is mentioned only when operations has approved an SLA (Q16).

``JsonlEscalationSink`` is the fallback for when the database itself is down: a hand-off that
needs the database cannot be recorded in the database. ``ingest_fallback_file`` loads it back.
"""

# Lazy annotations.
from __future__ import annotations

# hashlib makes the dedupe key.
import hashlib

# json stores context and fallback lines.
import json

# logging records an update that could not be queued.
import logging

# os.fsync forces fallback lines onto disk.
import os

# sqlite3 for the Connection type.
import sqlite3

# uuid makes case ids.
import uuid

# The type of the change a resolution makes.
from collections.abc import Callable

# datetime/timedelta for times and windows.
from datetime import datetime, timedelta

# Path for the fallback file.
from pathlib import Path

# Any/Literal/Protocol typing helpers.
from typing import Any, Literal, Protocol

# Frozen validated models and a field validator.
from pydantic import BaseModel, ConfigDict, Field, field_validator

# The injectable clock.
from setuhaul import chat_loads
from setuhaul.clock import Clock

# Settings give the SLA and reopen window.
from setuhaul.config import load_settings

# immediate(): the write transaction; parse_operational_timestamp for stored times.
from setuhaul.db import immediate, parse_operational_timestamp

# The outbox that reaches the driver's phone.
from setuhaul.events import queue_driver_notice

# Authorisation error.
from setuhaul.identity import AuthError

# Driver-facing wording (in three languages) for case updates.
from setuhaul.language import say

# Every permitted reason for a hand-off. A request with any other code is rejected, so queue statistics stay meaningful.
REASON_CODES = frozenset({
    "NO_SLOT", "CONTRADICTORY_DATA", "INCOMPLETE_FEASIBILITY_DATA", "APPROVAL_REQUIRED", "SAFETY",
    "REGULATED_LOAD", "POLICY_CONFLICT", "DATABASE_UNAVAILABLE", "MODEL_UNAVAILABLE", "MODEL_LOOP_LIMIT",
    "LANGUAGE_UNSUPPORTED", "CANCELLATION_CUTOFF", "CUSTOMER_COMMITMENT", "CUSTOMER_COMMITMENT_UNKNOWN",
    "DRIVER_REQUESTED", "OTHER_UNCERTAINTY",
    "PASS_CONFIRMATION_LOCKED", "VISIT_OVERSTAY",
})
# How a coordinator may close a case.
RESOLUTION_CODES = frozenset({"ASSIGNED_SLOT", "ADVISED_DRIVER", "CANCELLED_BY_OPERATIONS", "NO_ACTION_NEEDED", "OTHER"})
# The four case states.
Status = Literal["OPEN", "CLAIMED", "RESOLVED", "CANCELLED"]


# What the conversation produces when it cannot proceed on its own.
class HandoffRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    reason_code: str
    driver_id: str
    thread_id: str
    shipment_id: str | None = None
    summary: str = ""
    # Everything a coordinator needs: recent messages, language, tools used, ETA and so on.
    context: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    @field_validator("reason_code")
    @classmethod
    def _known(cls, v: str) -> str:
        # Reject unknown reason codes at construction time.
        if v not in REASON_CODES:
            raise ValueError(f"unknown escalation reason {v!r}")
        return v


# Anything that can record a hand-off and return a case id.
class EscalationSink(Protocol):
    def record(self, request: HandoffRequest) -> str: ...


# A sink that keeps requests in a list; used in tests and replay (the case id is just a running number).
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
        # Make sure the folder exists (the first hand-off may be the first write).
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # "a" appends, so earlier hand-offs are never overwritten.
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"case_id": case_id, **request.model_dump(mode="json")}, sort_keys=True) + "\n")
            # flush pushes Python's buffer to the OS; fsync forces the OS to write it to disk, so a crash cannot lose it.
            fh.flush()
            os.fsync(fh.fileno())
        return case_id


# ---- the SQL queue -------------------------------------------------------------------------------------
# A case as a coordinator or the API sees it.
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
    # Where the case came from (CONVERSATION, or FALLBACK_FILE when ingested after an outage).
    source: str
    created_at: datetime
    # When a response is due; None because no SLA has been approved (Q16).
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


# The answer to a claim, resolve, cancel or reopen attempt.
class TransitionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["CLAIMED", "ALREADY_CLAIMED", "NOT_OPEN", "NOT_FOUND", "RESOLVED", "NOT_CLAIMED",
                    "NOT_YOUR_CASE", "CANCELLED", "ALREADY_FINISHED", "REOPENED", "REOPEN_WINDOW_PASSED",
                    "NOT_RESOLVED", "INVALID_RESOLUTION"]
    case_id: str | None = None
    claimed_by: str | None = None
    replayed: bool = False


def _view(r: sqlite3.Row) -> CaseView:
    # Short alias for the timestamp parser.
    ts = parse_operational_timestamp
    # Convert a database row into a typed CaseView, parsing JSON and timestamps; empty timestamp columns become None.
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
    # Append one immutable event to the case history. Every state change calls this, so the log is complete.
    conn.execute(
        "insert into escalation_events (case_id, event_type, actor_type, actor_id, from_status, to_status, note, "
        "occurred_at) values (?,?,?,?,?,?,?,?)", (case_id, event_type, actor_type, actor_id, from_status, to_status, note,
                                                  now.isoformat()))


def _set_thread(conn: sqlite3.Connection, thread_id: str, status: str) -> None:
    # Mark the chat thread ESCALATED while a case is active and OPEN again when it ends.
    conn.execute("update chat_threads set thread_status=? where thread_id=?", (status, thread_id))


def _post_update(conn: sqlite3.Connection, case: sqlite3.Row, text: str, now: datetime) -> None:
    """Updates arrive in the driver's chat thread, as an OPERATIONS message."""
    # The message is written straight into chat_messages; memory's cache is rebuilt on the driver's next turn.
    with chat_loads.recording(conn, case["thread_id"], case["shipment_id"]):
        conn.execute(
            "insert into chat_messages (chat_message_id, thread_id, sender_type, message_text, message_ts) "
            "values (?,?, 'OPERATIONS', ?, ?)", ("MSG-" + uuid.uuid4().hex[:12].upper(), case["thread_id"], text, now.isoformat()))


def _lang(case: sqlite3.Row) -> str:
    # The language stored in the case context when the hand-off was made, defaulting to English.
    return str(json.loads(case["context_json"]).get("language") or "en")


def dedupe_key(req: HandoffRequest) -> str:
    # The same driver, thread, shipment and reason always produce the same key, so repeats can be folded into one case.
    raw = f"{req.driver_id}|{req.thread_id}|{req.shipment_id or ''}|{req.reason_code}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def create_handoff(conn: sqlite3.Connection, req: HandoffRequest, clock: Clock, source: str = "CONVERSATION") -> CaseCreated:
    """Open a case, or fold a duplicate request into the one already active for the same key."""
    key, now = dedupe_key(req), clock.now()
    with immediate(conn):
        # Is there already an active (open or claimed) case for the same driver/thread/shipment/reason?
        existing = conn.execute("select case_id from escalation_cases where dedupe_key=? and status in ('OPEN','CLAIMED')",
                                (key,)).fetchone()
        if existing is not None:
            # Yes: do not create a second case. Record the repeat on the first, so nothing is lost and the queue stays clean.
            _event(conn, existing["case_id"], "DUPLICATE_REQUEST", "SYSTEM", None, None, None, req.summary[:200], now)
            return CaseCreated(case_id=existing["case_id"], created=False)
        case_id = "ESC-" + uuid.uuid4().hex[:8].upper()
        # The SLA is None unless operations approved one; no due time is invented.
        sla = load_settings().escalation_sla_minutes
        conn.execute(
            "insert into escalation_cases (case_id, dedupe_key, status, reason_code, driver_id, thread_id, shipment_id, "
            "summary, context_json, source, created_at, sla_due_at, updated_at) values (?,?, 'OPEN',?,?,?,?,?,?,?,?,?,?)",
            (case_id, key, req.reason_code, req.driver_id, req.thread_id, req.shipment_id, req.summary,
             json.dumps(req.context, default=str, sort_keys=True), source, req.created_at.isoformat(),
             (req.created_at + timedelta(minutes=sla)).isoformat() if sla else None, now.isoformat()))
        # First history event, then flag the chat thread as escalated.
        _event(conn, case_id, "CREATED", "SYSTEM", None, None, "OPEN", req.reason_code, now)
        _set_thread(conn, req.thread_id, "ESCALATED")
    return CaseCreated(case_id=case_id, created=True)


class SqlEscalationSink:
    """The primary sink: hand-offs become queue cases. Falls back to the JSONL sink at the call site."""

    def __init__(self, conn: sqlite3.Connection, clock: Clock) -> None:
        self._conn, self._clock = conn, clock

    def record(self, request: HandoffRequest) -> str:
        # Create (or fold into) a queue case and return its id.
        return create_handoff(self._conn, request, self._clock).case_id


def ingest_fallback_file(conn: sqlite3.Connection, path: Path, clock: Clock) -> int:
    """Load hand-offs that were written while the database was down. Safe to run on every sweep."""
    path = Path(path)
    # The file is moved aside first, so hand-offs written meanwhile start a new file. One left over from a load that was
    # interrupted is finished before a new one is taken.
    loading = path.with_name(path.name + ".loading")
    if not loading.exists():
        if not path.exists():
            return 0
        path.rename(loading)
    n = 0
    broken: list[str] = []
    lines = loading.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines:
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            # The old case id was only a placeholder; a real one is assigned on ingest.
            d.pop("case_id", None)
            req = HandoffRequest.model_validate(d)
            # Duplicates fold into existing cases, so re-ingesting cannot flood the queue; count only new cases.
            if create_handoff(conn, req, clock, source="FALLBACK_FILE").created:
                n += 1
        # A line that can never be loaded (cut off mid-write, unknown thread) must not hold back the others.
        # A busy or broken database is not caught: the file stays and the next sweep tries again.
        except (ValueError, TypeError, AttributeError, sqlite3.IntegrityError):
            broken.append(line)
    if broken:
        _append_lines(path.with_name(path.name + ".rejected"), broken)
        logging.getLogger(__name__).error("%d fallback hand-off line(s) could not be loaded; see %s.rejected",
                                          len(broken), path.name)
    # Keep what was read, for the record, adding to any earlier outage's.
    _append_lines(path.with_name(path.name + ".ingested"), lines)
    loading.unlink()
    return n


def _append_lines(path: Path, lines: list[str]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.writelines(line + "\n" for line in lines)


def list_open_cases(conn: sqlite3.Connection, statuses: tuple[str, ...] = ("OPEN", "CLAIMED"), limit: int = 50) -> list[CaseView]:
    # One "?" per requested status for the IN (...) list.
    marks = ",".join("?" for _ in statuses)
    # Oldest first (the longest-waiting driver is at the top), capped at `limit`.
    rows = conn.execute(f"select * from escalation_cases where status in ({marks}) order by created_at, case_id limit ?",
                        (*statuses, limit)).fetchall()
    return [_view(r) for r in rows]


def get_case(conn: sqlite3.Connection, case_id: str) -> CaseView | None:
    r = conn.execute("select * from escalation_cases where case_id=?", (case_id,)).fetchone()
    return _view(r) if r else None


def case_events(conn: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    # The full event history of one case, in order.
    return [dict(r) for r in conn.execute("select * from escalation_events where case_id=? order by event_id", (case_id,))]


def claim_case(conn: sqlite3.Connection, case_id: str, coordinator_id: str, clock: Clock) -> TransitionResult:
    now = clock.now()
    with immediate(conn):
        c = conn.execute("select * from escalation_cases where case_id=?", (case_id,)).fetchone()
        if c is None:
            return TransitionResult(status="NOT_FOUND")
        # Already claimed: if by the same coordinator, it is a harmless repeat (replayed=True); if by someone else, say who.
        if c["status"] == "CLAIMED":
            return TransitionResult(status="ALREADY_CLAIMED", case_id=case_id, claimed_by=c["claimed_by"],
                                    replayed=c["claimed_by"] == coordinator_id)
        # Resolved or cancelled cases cannot be claimed.
        if c["status"] != "OPEN":
            return TransitionResult(status="NOT_OPEN", case_id=case_id)
        # The "and status='OPEN'" guard means two coordinators claiming at once cannot both succeed.
        conn.execute("update escalation_cases set status='CLAIMED', claimed_by=?, claimed_at=?, updated_at=? "
                     "where case_id=? and status='OPEN'", (coordinator_id, now.isoformat(), now.isoformat(), case_id))
        _event(conn, case_id, "CLAIMED", "COORDINATOR", coordinator_id, "OPEN", "CLAIMED", None, now)
        # Tell the driver in their chat, in their language.
        _post_update(conn, c, say("case_claimed", _lang(c), case=case_id), now)
    _to_phone(conn, c, say("case_claimed", _lang(c), case=case_id), now)
    return TransitionResult(status="CLAIMED", case_id=case_id, claimed_by=coordinator_id)


def _to_phone(conn: sqlite3.Connection, case: sqlite3.Row, text: str, now: datetime) -> None:
    """Queue the update for the driver's phone, after the change is saved. A case may name no load."""
    try:
        queue_driver_notice(conn, case["driver_id"], case["shipment_id"], text, now)
    # The case is already saved: a busy database must not turn that into an error.
    except sqlite3.Error:
        conn.rollback()
        logging.getLogger(__name__).warning("case update not queued for the phone", exc_info=True)


def resolve_case(conn: sqlite3.Connection, case_id: str, coordinator_id: str, resolution_code: str, note: str,
                 clock: Clock, apply: Callable[[sqlite3.Row], str] | None = None) -> TransitionResult:
    """`apply` makes the change the resolution promises, in the same transaction, and returns what to tell the driver."""
    # An unknown resolution code is refused before anything is touched.
    if resolution_code not in RESOLUTION_CODES:
        return TransitionResult(status="INVALID_RESOLUTION", case_id=case_id)
    now = clock.now()
    with immediate(conn):
        c = conn.execute("select * from escalation_cases where case_id=?", (case_id,)).fetchone()
        if c is None:
            return TransitionResult(status="NOT_FOUND")
        # Already closed.
        if c["status"] in ("RESOLVED", "CANCELLED"):
            return TransitionResult(status="ALREADY_FINISHED", case_id=case_id)
        # Must be claimed first.
        if c["status"] != "CLAIMED":
            return TransitionResult(status="NOT_CLAIMED", case_id=case_id)
        # Only the coordinator who claimed it may resolve it.
        if c["claimed_by"] != coordinator_id:
            return TransitionResult(status="NOT_YOUR_CASE", case_id=case_id, claimed_by=c["claimed_by"])
        done = apply(c) if apply is not None else ""
        if done:
            # The note is typed by hand and often has no full stop before the sentence added after it.
            stop = "." if note.strip()[-1:].isalnum() else ""
            note = f"{note.strip()}{stop} {done}".strip()
        conn.execute("update escalation_cases set status='RESOLVED', resolved_at=?, resolution_code=?, resolution_note=?, "
                     "updated_at=? where case_id=?", (now.isoformat(), resolution_code, note, now.isoformat(), case_id))
        _event(conn, case_id, "RESOLVED", "COORDINATOR", coordinator_id, "CLAIMED", "RESOLVED", f"{resolution_code}: {note}", now)
        # The driver sees the resolution note in the chat; the thread returns to normal.
        _post_update(conn, c, say("case_resolved", _lang(c), case=case_id, note=note), now)
        _set_thread(conn, c["thread_id"], "OPEN")
    _to_phone(conn, c, say("case_resolved", _lang(c), case=case_id, note=note), now)
    return TransitionResult(status="RESOLVED", case_id=case_id)


def cancel_case_by_driver(conn: sqlite3.Connection, case_id: str, driver_id: str, clock: Clock) -> TransitionResult:
    now = clock.now()
    with immediate(conn):
        c = conn.execute("select * from escalation_cases where case_id=?", (case_id,)).fetchone()
        # Not found and "someone else's case" give the same error.
        if c is None or c["driver_id"] != driver_id:
            raise AuthError("FOREIGN_CASE")
        if c["status"] in ("RESOLVED", "CANCELLED"):
            return TransitionResult(status="ALREADY_FINISHED", case_id=case_id)
        conn.execute("update escalation_cases set status='CANCELLED', updated_at=? where case_id=?", (now.isoformat(), case_id))
        # If a coordinator had already claimed it, the history says so, so their work is not silently discarded.
        note = f"driver cancelled while claimed by {c['claimed_by']}" if c["status"] == "CLAIMED" else "driver cancelled"
        _event(conn, case_id, "CANCELLED", "DRIVER", driver_id, c["status"], "CANCELLED", note, now)
        _post_update(conn, c, say("case_cancelled", _lang(c), case=case_id), now)
        _set_thread(conn, c["thread_id"], "OPEN")
    return TransitionResult(status="CANCELLED", case_id=case_id, claimed_by=c["claimed_by"])


def reopen_case(conn: sqlite3.Connection, case_id: str, coordinator_id: str, clock: Clock) -> TransitionResult:
    """Reopen policy: only a RESOLVED case, only inside the window. Afterwards a new hand-off opens a new case."""
    now = clock.now()
    # The reopen window (24 hours by default).
    window = timedelta(hours=load_settings().escalation_reopen_window_hours)
    with immediate(conn):
        c = conn.execute("select * from escalation_cases where case_id=?", (case_id,)).fetchone()
        if c is None:
            return TransitionResult(status="NOT_FOUND")
        # Only a resolved case can be reopened.
        if c["status"] != "RESOLVED":
            return TransitionResult(status="NOT_RESOLVED", case_id=case_id)
        # Too late: a new hand-off should open a new case instead.
        if now - parse_operational_timestamp(c["resolved_at"]) > window:
            return TransitionResult(status="REOPEN_WINDOW_PASSED", case_id=case_id)
        # If a newer case for the same problem is already active, do not create a second active one.
        clash = conn.execute("select 1 from escalation_cases where dedupe_key=? and status in ('OPEN','CLAIMED')",
                             (c["dedupe_key"],)).fetchone()
        if clash is not None:
            return TransitionResult(status="NOT_RESOLVED", case_id=case_id)  # a newer case is already active
        # Back to OPEN with ownership and resolution cleared, and the reopen counter incremented.
        conn.execute("update escalation_cases set status='OPEN', claimed_by=NULL, claimed_at=NULL, resolved_at=NULL, "
                     "resolution_code=NULL, resolution_note=NULL, reopened_count=reopened_count+1, updated_at=? "
                     "where case_id=?", (now.isoformat(), case_id))
        _event(conn, case_id, "REOPENED", "COORDINATOR", coordinator_id, "RESOLVED", "OPEN", None, now)
        _set_thread(conn, c["thread_id"], "ESCALATED")
    return TransitionResult(status="REOPENED", case_id=case_id)


# What a driver is told about their case.
class DriverCaseStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    status: Status
    text: str
    sla_minutes: int | None = None


def get_driver_case_status(conn: sqlite3.Connection, driver_id: str, thread_id: str | None = None,
                           language: str | None = None) -> DriverCaseStatus | None:
    """What the driver is told. No response time is mentioned unless an SLA is approved and set on the case."""
    # The driver's cases, optionally limited to one thread.
    sql = "select * from escalation_cases where driver_id=?" + (" and thread_id=?" if thread_id else "")
    args: tuple[str, ...] = (driver_id, thread_id) if thread_id else (driver_id,)
    # Active cases first (OPEN or CLAIMED sort before finished ones), then the newest.
    rows = conn.execute(sql + " order by (status in ('OPEN','CLAIMED')) desc, created_at desc limit 1", args).fetchall()
    if not rows:
        return None
    c = rows[0]
    # The language: the caller's, or the one stored with the case.
    lang = language or _lang(c)
    note = c["resolution_note"] or ""
    # Pick the wording by status. Each entry is a function so only the chosen one is evaluated.
    text = {
        "OPEN": lambda: say("case_status_open", lang, case=c["case_id"]),
        "CLAIMED": lambda: say("case_status_claimed", lang, case=c["case_id"]),
        "RESOLVED": lambda: say("case_resolved", lang, case=c["case_id"], note=note),
        "CANCELLED": lambda: say("case_cancelled", lang, case=c["case_id"]),
    }[c["status"]]()
    sla = load_settings().escalation_sla_minutes
    # A response time is mentioned only for active cases AND only when operations approved an SLA.
    if c["status"] in ("OPEN", "CLAIMED") and sla and c["sla_due_at"]:
        text += say("sla_suffix", lang, minutes=str(sla))
    return DriverCaseStatus(case_id=c["case_id"], status=c["status"], text=text,
                            sla_minutes=sla if c["sla_due_at"] else None)
