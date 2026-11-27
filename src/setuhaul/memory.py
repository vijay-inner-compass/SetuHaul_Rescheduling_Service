"""Bounded shared conversation memory.

SQL is the durable truth: full chat in ``chat_messages`` and structured state in
``conversation_state``. Redis only caches a bounded recent window under ``(driver_id, thread_id)``
and can be lost at any time; ``rebuild_from_durable_history`` reconstructs it.
"""

# Lazy annotations.
from __future__ import annotations

# json stores the cache blob and state as text.
import json

# logging says when the cache is unreachable.
import logging

# sqlite3 for the Connection type.
import sqlite3

# uuid makes thread and message ids.
import uuid

# Callable types the hold checker.
from collections.abc import Callable

# datetime for expiry and event times.
from datetime import datetime

# Any for the loosely typed cache blob; Literal for restricted strings.
from typing import Any, Literal

# Frozen validated models and field defaults.
from pydantic import BaseModel, ConfigDict, Field

# The Redis client type.
from redis import Redis
from redis.exceptions import RedisError

# The injectable clock.
from setuhaul.clock import Clock

# Settings give the memory limits.
from setuhaul.config import Settings, load_settings

# Re-exported in __all__ below.
from setuhaul.db import parse_operational_timestamp

# Authorisation error.
from setuhaul.identity import AuthError

# Who can write into a conversation thread.
SENDER_TYPES = ("DRIVER", "AGENT", "OPERATIONS", "WAREHOUSE", "SYSTEM")
# How senders map to model roles: the driver is "user", our assistant is "assistant", everyone else becomes "system".
_ROLE = {"DRIVER": "user", "AGENT": "assistant"}
# What a hold looks like when checked: still live, lapsed, or no longer there (released/confirmed).
HoldStatus = Literal["LIVE", "EXPIRED", "GONE"]
# A function that, given a hold id, reports its live status.
HoldChecker = Callable[[str], HoldStatus]


def cache_key(driver_id: str, thread_id: str) -> str:
    # The Redis key is scoped by BOTH driver and thread, so one driver's cache can never be read as another's.
    return f"setuhaul:conv:{driver_id}:{thread_id}"


# One message in the recent-conversation window.
_log = logging.getLogger(__name__)


class Turn(BaseModel):
    model_config = ConfigDict(frozen=True)

    # user / assistant / system, as the model sees it.
    role: str
    # The real sender (DRIVER, AGENT, OPERATIONS ...).
    sender_type: str
    text: str
    ts: str


# A reference to the hold the driver currently has.
class HoldRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    appointment_id: str
    expires_at: datetime
    created_message_id: str | None = None  # a hold may not be confirmed in the message that created it


# Something that happened to the driver's hold while they were away (it lapsed, or it was released).
class ConversationEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["HOLD_EXPIRED", "HOLD_RELEASED"]
    appointment_id: str
    at: datetime


class PendingCancel(BaseModel):
    """The system has asked "cancel this appointment?" and waits for the driver's next message."""

    model_config = ConfigDict(frozen=True)

    shipment_id: str
    appointment_id: str
    asked_message_id: str  # the yes must come in a later message than this one
    asked_at: datetime


class ConversationState(BaseModel):
    """Structured state. Never trimmed by the turn caps."""

    selected_shipment_id: str | None = None
    # Shipments that are still in play for this driver.
    active_shipment_ids: tuple[str, ...] = ()
    # The most recent menu shown; "take option 1" refers to this one.
    latest_option_set_id: str | None = None
    # Slots already held for this driver in the current search, so declining never offers one twice.
    offered_slot_ids: tuple[str, ...] = ()
    # The live hold, if any.
    hold: HoldRef | None = None
    # A cancellation the driver has been asked to say yes to.
    pending_cancel: PendingCancel | None = None
    # Events the driver has not been told about yet.
    pending_events: list[ConversationEvent] = Field(default_factory=list)
    language: str | None = None  # en | hi | hinglish, once established
    language_prompts: int = 0  # how many times the driver has been asked to choose a language


# Everything the conversation layer needs for one turn.
class ConversationContext(BaseModel):
    driver_id: str
    thread_id: str
    turns: list[Turn]
    state: ConversationState
    events: list[ConversationEvent]
    # True if this came from Redis, False if it was rebuilt from the database.
    from_cache: bool


class ConversationMemoryStore:
    def __init__(
        self,
        conn: sqlite3.Connection,
        redis: Redis,
        clock: Clock,
        settings: Settings | None = None,
        hold_checker: HoldChecker | None = None,
    ) -> None:
        self._db = conn
        self._redis = redis
        self._clock = clock
        # Memory limits (20 turns, 12,000 characters, 24 hours) come from settings.
        self._s = settings or load_settings()
        # How to ask "is this hold still live?". Without one, expiry time alone decides.
        self._hold_checker = hold_checker

    # ---- ownership ---------------------------------------------------------------------------
    def _require_thread(self, driver_id: str, thread_id: str) -> sqlite3.Row:
        row = self._db.execute(
            "select thread_id, driver_id, shipment_id from chat_threads where thread_id=?", (thread_id,)
        ).fetchone()
        # Missing thread and another driver's thread give the same error, so thread ids cannot be probed.
        if row is None or row["driver_id"] != driver_id:
            raise AuthError("FOREIGN_THREAD")
        assert isinstance(row, sqlite3.Row)
        return row

    def _owns_shipment(self, driver_id: str, shipment_id: str) -> None:
        # The shipment must belong to the driver.
        if self._db.execute(
            "select 1 from v_driver_shipments where shipment_id=? and driver_id=?", (shipment_id, driver_id)
        ).fetchone() is None:
            raise AuthError("FOREIGN_SHIPMENT")

    def _active_shipments(self, driver_id: str) -> tuple[str, ...]:
        # The driver's shipments that are not finished, in a fixed order.
        rows = self._db.execute(
            "select shipment_id from v_driver_shipments where driver_id=? "
            "and current_status not in ('COMPLETED','CANCELLED') order by shipment_id",
            (driver_id,),
        )
        return tuple(r[0] for r in rows)

    # ---- threads and messages ----------------------------------------------------------------
    def open_thread(self, driver_id: str, shipment_id: str | None = None) -> str:
        # If a shipment is named it must be the driver's own.
        if shipment_id is not None:
            self._owns_shipment(driver_id, shipment_id)
        thread_id = "THR-" + uuid.uuid4().hex[:10].upper()
        self._db.execute(
            "insert into chat_threads (thread_id, driver_id, shipment_id, opened_at, thread_status, "
            "thread_intent) values (?,?,?,?, 'OPEN', 'UNKNOWN')",
            (thread_id, driver_id, shipment_id, self._clock.now().isoformat()),
        )
        self._db.commit()
        return thread_id

    def append(
        self,
        driver_id: str,
        thread_id: str,
        sender_type: str,
        text: str,
        external_message_id: str | None = None,
    ) -> None:
        # Ownership first, then a sanity check on the sender type.
        self._require_thread(driver_id, thread_id)
        if sender_type not in SENDER_TYPES:
            raise ValueError(f"unknown sender_type {sender_type!r}")
        ts = self._clock.now().isoformat()
        # The database is written first and committed: it is the durable truth. The original text is stored unaltered.
        self._db.execute(
            "insert into chat_messages (chat_message_id, thread_id, sender_type, message_text, "
            "message_ts, external_message_id) values (?,?,?,?,?,?)",
            ("MSG-" + uuid.uuid4().hex[:12].upper(), thread_id, sender_type, text, ts, external_message_id),
        )
        self._db.commit()
        # Then update the Redis cache. If the cache is empty, rebuild it from the database
        # (which already includes this message); otherwise append to the cached window and trim.
        key = cache_key(driver_id, thread_id)
        raw = self._cached(key)
        if raw is None:  # cold cache: rebuild from SQL, which already contains this message
            self.rebuild_from_durable_history(driver_id, thread_id)
        else:
            blob = json.loads(raw)
            turns = [Turn(**t) for t in blob["turns"]] + [self._turn(sender_type, text, ts)]
            blob["turns"] = [t.model_dump() for t in self._trim(turns)]
            blob["upto"] = self._newest(thread_id)
            self._write_cache(key, blob)

    def invalidate(self, driver_id: str, thread_id: str) -> None:
        """Drop the cached window so the next load rebuilds from SQL (used when others write to the thread)."""
        try:
            self._redis.delete(cache_key(driver_id, thread_id))
        except RedisError:
            _log.warning("conversation cache unreachable; not invalidated")

    # ---- state -------------------------------------------------------------------------------
    def _read_state(self, driver_id: str, thread_id: str, thread: sqlite3.Row) -> ConversationState:
        row = self._db.execute(
            "select state_json from conversation_state where driver_id=? and thread_id=?",
            (driver_id, thread_id),
        ).fetchone()
        # A saved state wins; otherwise start fresh, pre-selecting the thread's shipment if it has one.
        if row is not None:
            return ConversationState.model_validate_json(row["state_json"])
        return ConversationState(selected_shipment_id=thread["shipment_id"])

    def save_structured_state(self, driver_id: str, thread_id: str, state: ConversationState) -> None:
        self._require_thread(driver_id, thread_id)
        # Upsert: insert the first time, update thereafter. SQL holds the truth.
        self._db.execute(
            "insert into conversation_state (driver_id, thread_id, state_json, updated_at) values (?,?,?,?) "
            "on conflict(driver_id, thread_id) do update set state_json=excluded.state_json, "
            "updated_at=excluded.updated_at",
            (driver_id, thread_id, state.model_dump_json(), self._clock.now().isoformat()),
        )
        self._db.commit()
        # Keep the cache in step if it exists (if not, the next load rebuilds it from SQL anyway).
        key = cache_key(driver_id, thread_id)
        raw = self._cached(key)
        if raw is not None:
            blob = json.loads(raw)
            blob["state"] = json.loads(state.model_dump_json())
            self._write_cache(key, blob)

    def select_shipment(self, driver_id: str, thread_id: str, shipment_id: str) -> None:
        # Both the thread and the shipment must belong to the driver.
        thread = self._require_thread(driver_id, thread_id)
        self._owns_shipment(driver_id, shipment_id)
        state = self._read_state(driver_id, thread_id, thread)
        self.save_structured_state(driver_id, thread_id, state.model_copy(update={"selected_shipment_id": shipment_id}))

    def acknowledge_events(self, driver_id: str, thread_id: str) -> None:
        # Called after the driver has been told about expiries/releases, so they are not repeated.
        thread = self._require_thread(driver_id, thread_id)
        state = self._read_state(driver_id, thread_id, thread)
        self.save_structured_state(driver_id, thread_id, state.model_copy(update={"pending_events": []}))

    # ---- cache -------------------------------------------------------------------------------
    @staticmethod
    def _turn(sender_type: str, text: str, ts: str) -> Turn:
        # Unknown senders (operations, warehouse, system) all appear to the model as role "system".
        return Turn(role=_ROLE.get(sender_type, "system"), sender_type=sender_type, text=text, ts=ts)

    def _trim(self, turns: list[Turn]) -> list[Turn]:
        # First cap by number of turns: keep only the most recent ones.
        kept = turns[-self._s.memory_max_turns :]
        # Then cap by total characters: drop the oldest turn until the window fits (always keep at least one).
        while len(kept) > 1 and sum(len(t.text) for t in kept) > self._s.memory_max_chars:
            kept = kept[1:]
        # A single enormous message is truncated rather than dropped.
        if kept and len(kept[0].text) > self._s.memory_max_chars:
            kept = [kept[0].model_copy(update={"text": kept[0].text[: self._s.memory_max_chars]})]
        return kept

    def _write_cache(self, key: str, blob: dict[str, Any]) -> None:
        # ex= sets Redis' own expiry (24 hours): an idle conversation's cache disappears by itself.
        try:
            self._redis.set(key, json.dumps(blob), ex=self._s.memory_ttl_seconds)
        except RedisError:
            _log.warning("conversation cache unreachable; not written")

    # The cache is only a copy: if Redis cannot be reached, carry on from the database.
    def _cached(self, key: str) -> str | None:
        try:
            raw: str | None = self._redis.get(key)  # type: ignore[assignment]
        except RedisError:
            _log.warning("conversation cache unreachable; reading the database")
            return None
        return raw

    # The newest message row of a thread: a cached window that stops short of it has missed something.
    def _newest(self, thread_id: str) -> int:
        row = self._db.execute("select max(rowid) from chat_messages where thread_id=?", (thread_id,)).fetchone()
        return int(row[0] or 0)

    def _history_turns(self, thread_id: str) -> list[Turn]:
        # Rebuild the window from the durable chat log, oldest first, skipping messages flagged as duplicates.
        rows = self._db.execute(
            "select sender_type, message_text, message_ts from chat_messages "
            "where thread_id=? and is_duplicate=0 order by message_ts, rowid",
            (thread_id,),
        )
        return self._trim([self._turn(r[0], r[1], r[2]) for r in rows])

    def rebuild_from_durable_history(self, driver_id: str, thread_id: str) -> ConversationContext:
        thread = self._require_thread(driver_id, thread_id)
        turns = self._history_turns(thread_id)
        state = self._read_state(driver_id, thread_id, thread)
        # Refill the cache from the database.
        self._write_cache(
            cache_key(driver_id, thread_id),
            {
                "identity": {"driver_id": driver_id, "thread_id": thread_id},
                "state": json.loads(state.model_dump_json()),
                "turns": [t.model_dump() for t in turns],
                "upto": self._newest(thread_id),
            },
        )
        return self._finish(driver_id, thread_id, turns, state, from_cache=False)

    def load(self, driver_id: str, thread_id: str) -> ConversationContext:
        thread = self._require_thread(driver_id, thread_id)
        raw = self._cached(cache_key(driver_id, thread_id))
        # Cache miss (expired, evicted, or Redis restarted): rebuild from SQL. Nothing is lost.
        if raw is None:
            return self.rebuild_from_durable_history(driver_id, thread_id)
        # Cache hit: use the cached messages, unless something was written that the cache never saw (an outage, or a
        # message added directly to the database).
        blob = json.loads(raw)
        if blob.get("upto") != self._newest(thread_id):
            return self.rebuild_from_durable_history(driver_id, thread_id)
        turns = [Turn(**t) for t in blob["turns"]]
        # State always comes from the database: a copy that missed a write must never bring back an old hold or question.
        state = self._read_state(driver_id, thread_id, thread)
        return self._finish(driver_id, thread_id, turns, state, from_cache=True)

    # ---- hold reconciliation -----------------------------------------------------------------
    def _hold_status(self, hold: HoldRef) -> HoldStatus:
        # Prefer the database's answer (via the checker); without one, fall back to the stored expiry time.
        if self._hold_checker is not None:
            return self._hold_checker(hold.appointment_id)
        return "EXPIRED" if hold.expires_at <= self._clock.now() else "LIVE"

    def _finish(
        self, driver_id: str, thread_id: str, turns: list[Turn], state: ConversationState, from_cache: bool
    ) -> ConversationContext:
        # If the state says there is a hold, verify it is still live: a remembered hold is a claim, not a fact.
        if state.hold is not None:
            status = self._hold_status(state.hold)
            if status != "LIVE":
                # It lapsed or was released: forget it and record an event so the driver is told exactly once.
                event = ConversationEvent(
                    type="HOLD_EXPIRED" if status == "EXPIRED" else "HOLD_RELEASED",
                    appointment_id=state.hold.appointment_id,
                    at=self._clock.now(),
                )
                state = state.model_copy(update={"hold": None, "pending_events": [*state.pending_events, event]})
                self.save_structured_state(driver_id, thread_id, state)
        # Always refresh the list of active shipments from the database, never from cache.
        update: dict[str, Any] = {"active_shipment_ids": self._active_shipments(driver_id)}
        # A remembered selection is dropped while that shipment waits for its order number.
        if state.selected_shipment_id is not None and self._db.execute(
                "select 1 from shipment_assignments where shipment_id=? and confirmed_at is null",
                (state.selected_shipment_id,)).fetchone() is not None:
            update["selected_shipment_id"] = None
        live = state.model_copy(update=update)
        return ConversationContext(
            driver_id=driver_id,
            thread_id=thread_id,
            turns=turns,
            state=live,
            events=list(state.pending_events),
            from_cache=from_cache,
        )


__all__ = [
    "ConversationContext",
    "ConversationEvent",
    "ConversationMemoryStore",
    "ConversationState",
    "HoldRef",
    "Turn",
    "cache_key",
    "parse_operational_timestamp",
]
