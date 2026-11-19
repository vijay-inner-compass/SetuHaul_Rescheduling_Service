"""Phase 3: bounded shared conversation memory; durable truth lives in SQL."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import fakeredis
import pytest

from setuhaul.clock import FakeClock
from setuhaul.config import load_settings
from setuhaul.identity import AuthError
from setuhaul.memory import ConversationMemoryStore, HoldRef, cache_key


def make_store(db: sqlite3.Connection, redis: fakeredis.FakeRedis, clock: FakeClock, **kw) -> ConversationMemoryStore:
    return ConversationMemoryStore(db, redis, clock, load_settings(), **kw)


@pytest.fixture()
def store(db: sqlite3.Connection, redis_client: fakeredis.FakeRedis, clock: FakeClock) -> ConversationMemoryStore:
    return make_store(db, redis_client, clock)


@pytest.fixture()
def thread(store: ConversationMemoryStore) -> str:
    return store.open_thread("DRV006", shipment_id="SHP1006")


def say(store: ConversationMemoryStore, thread: str, n: int, driver: str = "DRV006", size: int = 10) -> None:
    for i in range(n):
        sender = "DRIVER" if i % 2 == 0 else "AGENT"
        store.append(driver, thread, sender, f"m{i:03d}".ljust(size, "x"))


# --- basics ----------------------------------------------------------------------------------
def test_open_thread_creates_owned_row(store: ConversationMemoryStore, db: sqlite3.Connection, thread: str) -> None:
    row = db.execute("select driver_id, shipment_id, thread_status from chat_threads where thread_id=?", (thread,)).fetchone()
    assert tuple(row) == ("DRV006", "SHP1006", "OPEN")


def test_open_thread_for_foreign_shipment_rejected(store: ConversationMemoryStore) -> None:
    with pytest.raises(AuthError) as err:
        store.open_thread("DRV006", shipment_id="SHP1012")
    assert err.value.code == "FOREIGN_SHIPMENT"


def test_turns_round_trip_in_order(store: ConversationMemoryStore, thread: str) -> None:
    say(store, thread, 4)
    ctx = store.load("DRV006", thread)
    assert [t.text[:4] for t in ctx.turns] == ["m000", "m001", "m002", "m003"]
    assert [t.role for t in ctx.turns] == ["user", "assistant", "user", "assistant"]
    assert ctx.from_cache is True


def test_cache_key_is_scoped_to_driver_and_thread() -> None:
    assert cache_key("DRV006", "THR9") != cache_key("DRV007", "THR9")
    assert "DRV006" in cache_key("DRV006", "THR9") and "THR9" in cache_key("DRV006", "THR9")


# --- caps and TTL ----------------------------------------------------------------------------
def test_cache_caps_at_20_turns_but_sql_keeps_all(
    store: ConversationMemoryStore, db: sqlite3.Connection, thread: str
) -> None:
    say(store, thread, 25)
    ctx = store.load("DRV006", thread)
    assert len(ctx.turns) == 20
    assert ctx.turns[0].text.startswith("m005") and ctx.turns[-1].text.startswith("m024")
    sql_count = db.execute("select count(*) from chat_messages where thread_id=?", (thread,)).fetchone()[0]
    assert sql_count == 25


def test_cache_caps_at_12000_chars_keeping_newest(store: ConversationMemoryStore, thread: str) -> None:
    say(store, thread, 8, size=3000)
    ctx = store.load("DRV006", thread)
    assert sum(len(t.text) for t in ctx.turns) <= 12_000
    assert ctx.turns[-1].text.startswith("m007")
    assert len(ctx.turns) == 4


def test_single_oversized_turn_is_truncated_not_dropped(store: ConversationMemoryStore, thread: str) -> None:
    store.append("DRV006", thread, "DRIVER", "y" * 20_000)
    ctx = store.load("DRV006", thread)
    assert len(ctx.turns) == 1 and len(ctx.turns[0].text) <= 12_000


def test_cache_has_24h_ttl(store: ConversationMemoryStore, redis_client: fakeredis.FakeRedis, thread: str) -> None:
    say(store, thread, 1)
    ttl = redis_client.ttl(cache_key("DRV006", thread))
    assert 86_000 < ttl <= 86_400


def test_trimming_never_removes_identity_or_selected_shipment(
    store: ConversationMemoryStore, redis_client: fakeredis.FakeRedis, thread: str
) -> None:
    store.select_shipment("DRV006", thread, "SHP1021")
    say(store, thread, 40, size=2000)
    raw = json.loads(redis_client.get(cache_key("DRV006", thread)))
    assert raw["identity"]["driver_id"] == "DRV006"
    assert raw["state"]["selected_shipment_id"] == "SHP1021"
    assert len(raw["turns"]) < 40


# --- isolation -------------------------------------------------------------------------------
def test_other_driver_cannot_load_or_append(store: ConversationMemoryStore, thread: str) -> None:
    say(store, thread, 2)
    with pytest.raises(AuthError) as err:
        store.load("DRV007", thread)
    assert err.value.code == "FOREIGN_THREAD"
    with pytest.raises(AuthError):
        store.append("DRV007", thread, "DRIVER", "hi")
    with pytest.raises(AuthError):
        store.load("DRV006", "THR-DOES-NOT-EXIST")


# --- two shipments in one thread -------------------------------------------------------------
def test_two_active_shipments_explicit_switching(
    store: ConversationMemoryStore, db: sqlite3.Connection, thread: str
) -> None:
    ctx = store.load("DRV006", thread)
    assert ctx.state.selected_shipment_id == "SHP1006"
    assert set(ctx.state.active_shipment_ids) == {"SHP1006", "SHP1021"}
    store.select_shipment("DRV006", thread, "SHP1021")
    assert store.load("DRV006", thread).state.selected_shipment_id == "SHP1021"
    store.select_shipment("DRV006", thread, "SHP1006")
    assert store.load("DRV006", thread).state.selected_shipment_id == "SHP1006"
    owners = dict(db.execute("select shipment_id, driver_id from shipments where shipment_id in ('SHP1006','SHP1021')").fetchall())
    assert owners == {"SHP1006": "DRV006", "SHP1021": "DRV006"}


def test_selecting_foreign_shipment_rejected_and_state_unchanged(store: ConversationMemoryStore, thread: str) -> None:
    with pytest.raises(AuthError) as err:
        store.select_shipment("DRV006", thread, "SHP1012")
    assert err.value.code == "FOREIGN_SHIPMENT"
    assert store.load("DRV006", thread).state.selected_shipment_id == "SHP1006"


def test_selecting_completed_shipment_is_not_offered_as_active(store: ConversationMemoryStore) -> None:
    t = store.open_thread("DRV001")  # SHP1001 completed, SHP1017 in transit
    assert store.load("DRV001", t).state.active_shipment_ids == ("SHP1017",)


# --- Redis loss, restart, expiry -------------------------------------------------------------
def test_redis_loss_rebuilds_from_sql(
    store: ConversationMemoryStore, redis_client: fakeredis.FakeRedis, thread: str
) -> None:
    store.select_shipment("DRV006", thread, "SHP1021")
    say(store, thread, 6)
    before = store.load("DRV006", thread)
    redis_client.flushall()
    after = store.load("DRV006", thread)
    assert after.from_cache is False
    assert [t.text for t in after.turns] == [t.text for t in before.turns]
    assert after.state.selected_shipment_id == "SHP1021"
    assert store.load("DRV006", thread).from_cache is True  # repopulated


def test_restart_new_worker_new_cache_same_context(
    db: sqlite3.Connection, clock: FakeClock, redis_client: fakeredis.FakeRedis, thread: str
) -> None:
    first = make_store(db, redis_client, clock)
    first.select_shipment("DRV006", thread, "SHP1021")
    say(first, thread, 5)
    second = make_store(db, fakeredis.FakeRedis(decode_responses=True), clock)
    ctx = second.load("DRV006", thread)
    assert len(ctx.turns) == 5 and ctx.state.selected_shipment_id == "SHP1021"


def test_cache_expiry_rebuilds(store: ConversationMemoryStore, redis_client: fakeredis.FakeRedis, thread: str) -> None:
    say(store, thread, 3)
    redis_client.delete(cache_key("DRV006", thread))  # what TTL expiry does
    ctx = store.load("DRV006", thread)
    assert ctx.from_cache is False and len(ctx.turns) == 3


def test_rebuild_applies_the_same_caps(store: ConversationMemoryStore, redis_client: fakeredis.FakeRedis, thread: str) -> None:
    say(store, thread, 30)
    redis_client.flushall()
    ctx = store.rebuild_from_durable_history("DRV006", thread)
    assert len(ctx.turns) == 20


def test_same_timestamp_messages_keep_insertion_order(store: ConversationMemoryStore, redis_client: fakeredis.FakeRedis, thread: str) -> None:
    say(store, thread, 6)  # FakeClock does not advance: identical timestamps
    redis_client.flushall()
    ctx = store.load("DRV006", thread)
    assert [t.text[:4] for t in ctx.turns] == [f"m{i:03d}" for i in range(6)]


# --- hold expiry on return -------------------------------------------------------------------
def test_return_before_hold_expiry_has_no_event(store: ConversationMemoryStore, clock: FakeClock, thread: str) -> None:
    ctx = store.load("DRV006", thread)
    hold = HoldRef(appointment_id="APT-H1", expires_at=clock.now() + timedelta(minutes=3))
    store.save_structured_state("DRV006", thread, ctx.state.model_copy(update={"hold": hold}))
    clock.advance(timedelta(minutes=2))
    again = store.load("DRV006", thread)
    assert again.events == [] and again.state.hold == hold


@pytest.mark.parametrize("advance_s", [180, 181, 3600])
def test_return_at_or_after_expiry_yields_hold_expired_not_stale_promise(
    store: ConversationMemoryStore, clock: FakeClock, thread: str, advance_s: int
) -> None:
    store.append("DRV006", thread, "AGENT", "I am holding the 11:00 slot for you.")
    ctx = store.load("DRV006", thread)
    hold = HoldRef(appointment_id="APT-H1", expires_at=clock.now() + timedelta(minutes=3))
    store.save_structured_state("DRV006", thread, ctx.state.model_copy(update={"hold": hold}))
    clock.advance(timedelta(seconds=advance_s))
    back = store.load("DRV006", thread)
    assert [e.type for e in back.events] == ["HOLD_EXPIRED"]
    assert back.events[0].appointment_id == "APT-H1"
    assert back.state.hold is None


def test_hold_expired_event_persists_until_acknowledged(store: ConversationMemoryStore, clock: FakeClock, thread: str) -> None:
    ctx = store.load("DRV006", thread)
    hold = HoldRef(appointment_id="APT-H1", expires_at=clock.now() + timedelta(minutes=3))
    store.save_structured_state("DRV006", thread, ctx.state.model_copy(update={"hold": hold}))
    clock.advance(timedelta(minutes=4))
    assert len(store.load("DRV006", thread).events) == 1
    assert len(store.load("DRV006", thread).events) == 1  # reply may have failed; still pending
    store.acknowledge_events("DRV006", thread)
    assert store.load("DRV006", thread).events == []


def test_live_hold_state_overrides_clock_when_checker_says_expired(
    db: sqlite3.Connection, redis_client: fakeredis.FakeRedis, clock: FakeClock
) -> None:
    store = make_store(db, redis_client, clock, hold_checker=lambda appointment_id: "EXPIRED")
    t = store.open_thread("DRV006")
    ctx = store.load("DRV006", t)
    hold = HoldRef(appointment_id="APT-H1", expires_at=clock.now() + timedelta(minutes=3))
    store.save_structured_state("DRV006", t, ctx.state.model_copy(update={"hold": hold}))
    assert [e.type for e in store.load("DRV006", t).events] == ["HOLD_EXPIRED"]


def test_hold_expiry_detected_after_redis_loss(
    store: ConversationMemoryStore, redis_client: fakeredis.FakeRedis, clock: FakeClock, thread: str
) -> None:
    ctx = store.load("DRV006", thread)
    hold = HoldRef(appointment_id="APT-H1", expires_at=clock.now() + timedelta(minutes=3))
    store.save_structured_state("DRV006", thread, ctx.state.model_copy(update={"hold": hold}))
    redis_client.flushall()
    clock.advance(timedelta(minutes=10))
    assert [e.type for e in store.load("DRV006", thread).events] == ["HOLD_EXPIRED"]


def test_latest_option_set_id_is_kept_in_state(store: ConversationMemoryStore, thread: str) -> None:
    ctx = store.load("DRV006", thread)
    store.save_structured_state("DRV006", thread, ctx.state.model_copy(update={"latest_option_set_id": "OPT-1"}))
    assert store.load("DRV006", thread).state.latest_option_set_id == "OPT-1"
