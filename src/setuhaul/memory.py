"""Bounded shared conversation memory.

SQL is the durable truth: full chat in ``chat_messages`` and structured state in
``conversation_state``. Redis only caches a bounded recent window under ``(driver_id, thread_id)``
and can be lost at any time; ``rebuild_from_durable_history`` reconstructs it.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from redis import Redis

from setuhaul.clock import Clock
from setuhaul.config import Settings, load_settings
from setuhaul.db import parse_operational_timestamp
from setuhaul.identity import AuthError

SENDER_TYPES = ("DRIVER", "AGENT", "OPERATIONS", "WAREHOUSE", "SYSTEM")
_ROLE = {"DRIVER": "user", "AGENT": "assistant"}
HoldStatus = Literal["LIVE", "EXPIRED", "GONE"]
HoldChecker = Callable[[str], HoldStatus]


def cache_key(driver_id: str, thread_id: str) -> str:
    return f"setuhaul:conv:{driver_id}:{thread_id}"


class Turn(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: str
    sender_type: str
    text: str
    ts: str


class HoldRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    appointment_id: str
    expires_at: datetime
    created_message_id: str | None = None  # a hold may not be confirmed in the message that created it


class ConversationEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["HOLD_EXPIRED", "HOLD_RELEASED"]
    appointment_id: str
    at: datetime


class ConversationState(BaseModel):
    """Structured state. Never trimmed by the turn caps."""

    selected_shipment_id: str | None = None
    active_shipment_ids: tuple[str, ...] = ()
    latest_option_set_id: str | None = None
    hold: HoldRef | None = None
    pending_events: list[ConversationEvent] = Field(default_factory=list)
    language: str | None = None  # en | hi | hinglish, once established
    language_prompts: int = 0  # how many times the driver has been asked to choose a language


class ConversationContext(BaseModel):
    driver_id: str
    thread_id: str
    turns: list[Turn]
    state: ConversationState
    events: list[ConversationEvent]
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
        self._s = settings or load_settings()
        self._hold_checker = hold_checker

    # ---- ownership ---------------------------------------------------------------------------
    def _require_thread(self, driver_id: str, thread_id: str) -> sqlite3.Row:
        row = self._db.execute(
            "select thread_id, driver_id, shipment_id from chat_threads where thread_id=?", (thread_id,)
        ).fetchone()
        if row is None or row["driver_id"] != driver_id:
            raise AuthError("FOREIGN_THREAD")
        assert isinstance(row, sqlite3.Row)
        return row

    def _owns_shipment(self, driver_id: str, shipment_id: str) -> None:
        if self._db.execute(
            "select 1 from shipments where shipment_id=? and driver_id=?", (shipment_id, driver_id)
        ).fetchone() is None:
            raise AuthError("FOREIGN_SHIPMENT")

    def _active_shipments(self, driver_id: str) -> tuple[str, ...]:
        rows = self._db.execute(
            "select shipment_id from shipments where driver_id=? "
            "and current_status not in ('COMPLETED','CANCELLED') order by shipment_id",
            (driver_id,),
        )
        return tuple(r[0] for r in rows)

    # ---- threads and messages ----------------------------------------------------------------
    def open_thread(self, driver_id: str, shipment_id: str | None = None) -> str:
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
        self._require_thread(driver_id, thread_id)
        if sender_type not in SENDER_TYPES:
            raise ValueError(f"unknown sender_type {sender_type!r}")
        ts = self._clock.now().isoformat()
        self._db.execute(
            "insert into chat_messages (chat_message_id, thread_id, sender_type, message_text, "
            "message_ts, external_message_id) values (?,?,?,?,?,?)",
            ("MSG-" + uuid.uuid4().hex[:12].upper(), thread_id, sender_type, text, ts, external_message_id),
        )
        self._db.commit()
        key = cache_key(driver_id, thread_id)
        raw = self._redis.get(key)
        if raw is None:  # cold cache: rebuild from SQL, which already contains this message
            self.rebuild_from_durable_history(driver_id, thread_id)
        else:
            blob = json.loads(raw)
            turns = [Turn(**t) for t in blob["turns"]] + [self._turn(sender_type, text, ts)]
            blob["turns"] = [t.model_dump() for t in self._trim(turns)]
            self._write_cache(key, blob)

    def invalidate(self, driver_id: str, thread_id: str) -> None:
        """Drop the cached window so the next load rebuilds from SQL (used when others write to the thread)."""
        self._redis.delete(cache_key(driver_id, thread_id))

    # ---- state -------------------------------------------------------------------------------
    def _read_state(self, driver_id: str, thread_id: str, thread: sqlite3.Row) -> ConversationState:
        row = self._db.execute(
            "select state_json from conversation_state where driver_id=? and thread_id=?",
            (driver_id, thread_id),
        ).fetchone()
        if row is not None:
            return ConversationState.model_validate_json(row["state_json"])
        return ConversationState(selected_shipment_id=thread["shipment_id"])

    def save_structured_state(self, driver_id: str, thread_id: str, state: ConversationState) -> None:
        self._require_thread(driver_id, thread_id)
        self._db.execute(
            "insert into conversation_state (driver_id, thread_id, state_json, updated_at) values (?,?,?,?) "
            "on conflict(driver_id, thread_id) do update set state_json=excluded.state_json, "
            "updated_at=excluded.updated_at",
            (driver_id, thread_id, state.model_dump_json(), self._clock.now().isoformat()),
        )
        self._db.commit()
        key = cache_key(driver_id, thread_id)
        raw = self._redis.get(key)
        if raw is not None:
            blob = json.loads(raw)
            blob["state"] = json.loads(state.model_dump_json())
            self._write_cache(key, blob)

    def select_shipment(self, driver_id: str, thread_id: str, shipment_id: str) -> None:
        thread = self._require_thread(driver_id, thread_id)
        self._owns_shipment(driver_id, shipment_id)
        state = self._read_state(driver_id, thread_id, thread)
        self.save_structured_state(driver_id, thread_id, state.model_copy(update={"selected_shipment_id": shipment_id}))

    def acknowledge_events(self, driver_id: str, thread_id: str) -> None:
        thread = self._require_thread(driver_id, thread_id)
        state = self._read_state(driver_id, thread_id, thread)
        self.save_structured_state(driver_id, thread_id, state.model_copy(update={"pending_events": []}))

    # ---- cache -------------------------------------------------------------------------------
    @staticmethod
    def _turn(sender_type: str, text: str, ts: str) -> Turn:
        return Turn(role=_ROLE.get(sender_type, "system"), sender_type=sender_type, text=text, ts=ts)

    def _trim(self, turns: list[Turn]) -> list[Turn]:
        kept = turns[-self._s.memory_max_turns :]
        while len(kept) > 1 and sum(len(t.text) for t in kept) > self._s.memory_max_chars:
            kept = kept[1:]
        if kept and len(kept[0].text) > self._s.memory_max_chars:
            kept = [kept[0].model_copy(update={"text": kept[0].text[: self._s.memory_max_chars]})]
        return kept

    def _write_cache(self, key: str, blob: dict[str, Any]) -> None:
        self._redis.set(key, json.dumps(blob), ex=self._s.memory_ttl_seconds)

    def _history_turns(self, thread_id: str) -> list[Turn]:
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
        self._write_cache(
            cache_key(driver_id, thread_id),
            {
                "identity": {"driver_id": driver_id, "thread_id": thread_id},
                "state": json.loads(state.model_dump_json()),
                "turns": [t.model_dump() for t in turns],
            },
        )
        return self._finish(driver_id, thread_id, turns, state, from_cache=False)

    def load(self, driver_id: str, thread_id: str) -> ConversationContext:
        self._require_thread(driver_id, thread_id)
        raw = self._redis.get(cache_key(driver_id, thread_id))
        if raw is None:
            return self.rebuild_from_durable_history(driver_id, thread_id)
        blob = json.loads(raw)
        turns = [Turn(**t) for t in blob["turns"]]
        state = ConversationState.model_validate(blob["state"])
        return self._finish(driver_id, thread_id, turns, state, from_cache=True)

    # ---- hold reconciliation -----------------------------------------------------------------
    def _hold_status(self, hold: HoldRef) -> HoldStatus:
        if self._hold_checker is not None:
            return self._hold_checker(hold.appointment_id)
        return "EXPIRED" if hold.expires_at <= self._clock.now() else "LIVE"

    def _finish(
        self, driver_id: str, thread_id: str, turns: list[Turn], state: ConversationState, from_cache: bool
    ) -> ConversationContext:
        if state.hold is not None:
            status = self._hold_status(state.hold)
            if status != "LIVE":
                event = ConversationEvent(
                    type="HOLD_EXPIRED" if status == "EXPIRED" else "HOLD_RELEASED",
                    appointment_id=state.hold.appointment_id,
                    at=self._clock.now(),
                )
                state = state.model_copy(update={"hold": None, "pending_events": [*state.pending_events, event]})
                self.save_structured_state(driver_id, thread_id, state)
        live = state.model_copy(update={"active_shipment_ids": self._active_shipments(driver_id)})
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
