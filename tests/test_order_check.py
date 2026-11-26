"""The driver confirms an assigned shipment by sending its order number; the app checks it, never the model."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import fakeredis
import pytest
from fastapi.testclient import TestClient

from setuhaul.api import AppDeps, create_app
from setuhaul.auth import telegram
from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection
from setuhaul.identity import provision_identities
from setuhaul.language import say
from setuhaul.model_gateway import ModelMessage, ModelPolicy, ModelResponse
from setuhaul.rate_limit import RateLimits
from tests.helpers import at, seed_everything_known
from tests.shipment_helpers import mark_unconfirmed
from tests.test_telegram_channel import SECRET, bind, post, text_update

SHIP, DRIVER, NUMBER = "SHP1006", "DRV006", "ORD-261127-001"


class Recorder:
    """A model stand-in that remembers every prompt it was given."""

    def __init__(self) -> None:
        self.calls: list[Sequence[ModelMessage]] = []

    def complete(self, messages: Sequence[ModelMessage], tools: Sequence[dict[str, Any]], policy: ModelPolicy) -> ModelResponse:
        self.calls.append(tuple(messages))
        return ModelResponse(text="Tell me more.")


class Chat:
    def __init__(self, tmp_path: Path) -> None:
        self.path = make_working_db(tmp_path / "oc.sqlite3")
        conn = open_connection(path=self.path)
        seed_everything_known(conn)
        provision_identities(conn)
        conn.execute("update shipments set order_reference=? where shipment_id=?", (NUMBER, SHIP))
        conn.commit()
        conn.close()
        self.model = Recorder()
        self.bot = telegram.FakeTelegramClient()
        deps = AppDeps(db_path=self.path, redis=fakeredis.FakeRedis(decode_responses=True), webhook_secret=b"w",
                       coordinator_tokens={}, fallback_path=tmp_path / "f.jsonl", policy=ModelPolicy(provider="stub", model_id="stub"),
                       gateway_factory=lambda: self.model, clock=FakeClock(at("09:35")), rate_limits=RateLimits(50, 200),
                       telegram_secret=SECRET.encode(), telegram=self.bot)
        self.client = TestClient(create_app(deps))
        bind(self.client)

    def send(self, text: str, update_id: int | None = None) -> str:
        before = len(self.bot.sent)
        assert post(self.client, text_update(text, update_id=update_id)).status_code == 200
        return self.bot.sent[-1][1] if len(self.bot.sent) > before else ""

    def query(self, sql: str, *args: object) -> list[sqlite3.Row]:
        conn = open_connection(path=self.path)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def wait_for(self, shipment_id: str = SHIP) -> None:
        conn = open_connection(path=self.path)
        try:
            mark_unconfirmed(conn, shipment_id)
        finally:
            conn.close()

    def row(self, shipment_id: str = SHIP) -> sqlite3.Row:
        return self.query("select * from shipment_assignments where shipment_id=?", shipment_id)[0]

    def prompt(self) -> str:
        return self.model.calls[-1][0].content


@pytest.fixture()
def chat(tmp_path: Path) -> Chat:
    return Chat(tmp_path)


def test_the_right_number_confirms_without_calling_the_model(chat: Chat) -> None:
    chat.wait_for()
    reply = chat.send("ord 261127 001")
    assert NUMBER in reply and "SetuHaul" in reply and chat.model.calls == []
    assert chat.row()["confirmed_at"] is not None
    saved = chat.query("select m.sender_type, m.message_text from chat_messages m join chat_threads t using(thread_id) "
                       "where t.driver_id=? order by m.rowid desc limit 2", DRIVER)
    assert [(r["sender_type"], r["message_text"]) for r in reversed(saved)] == [("DRIVER", "ord 261127 001"), ("AGENT", reply)]


def test_fifth_wrong_number_locks_and_opens_one_case(chat: Chat) -> None:
    chat.wait_for()
    for _ in range(4):
        assert chat.send("ORD-999999-999") == say("order_mismatch", "en")
    assert chat.send("ORD-999999-999") == say("order_locked", "en")
    cases = chat.query("select shipment_id from escalation_cases where reason_code='ORDER_CONFIRMATION_LOCKED'")
    assert [c["shipment_id"] for c in cases] == [SHIP]
    assert chat.send(NUMBER) == say("order_locked", "en")
    assert chat.send("ORD-999999-999") == say("order_locked", "en")
    assert chat.row()["confirmed_at"] is None and chat.row()["failed_attempts"] == 5
    assert len(chat.query("select 1 from escalation_cases where reason_code='ORDER_CONFIRMATION_LOCKED'")) == 1


def test_a_replayed_message_counts_once(chat: Chat) -> None:
    chat.wait_for()
    chat.send("ORD-999999-999", update_id=7001)
    chat.send("ORD-999999-999", update_id=7001)
    assert chat.row()["failed_attempts"] == 1


def test_another_drivers_number_reads_like_any_wrong_one(chat: Chat) -> None:
    chat.wait_for()
    other = chat.query("select order_reference from shipments where driver_id<>? order by shipment_id limit 1", DRIVER)[0][0]
    assert chat.send(other) == say("order_mismatch", "en")


def test_other_text_reaches_the_model_with_no_details(chat: Chat) -> None:
    chat.wait_for()
    reply = chat.send("my truck broke down")
    assert reply.startswith("Tell me more.") and reply.endswith(say("order_waiting", "en"))
    assert "shipment_waiting_for_order_number" in chat.prompt()
    assert SHIP not in chat.prompt() and NUMBER not in chat.prompt()


def test_digits_without_ord_are_not_an_attempt(chat: Chat) -> None:
    chat.wait_for()
    assert chat.send("my number is 261127001").endswith(say("order_waiting", "en"))
    assert chat.row()["failed_attempts"] == 0


def test_no_waiting_shipment_changes_nothing(chat: Chat) -> None:
    reply = chat.send("my order is " + NUMBER)
    assert reply == "Tell me more." and "shipment_waiting_for_order_number" not in chat.prompt()


def test_one_message_confirms_every_number_that_matches(chat: Chat) -> None:
    second = chat.query("select shipment_id from shipments where driver_id<>? order by shipment_id limit 1", DRIVER)[0][0]
    conn = open_connection(path=chat.path)
    conn.execute("update shipments set driver_id=?, order_reference='ORD-261127-002' where shipment_id=?", (DRIVER, second))
    conn.commit()
    conn.close()
    chat.wait_for()
    chat.wait_for(second)
    reply = chat.send("ORD-261127-001, ORD-261127-002 and ORD-999999-999")
    assert NUMBER in reply and "ORD-261127-002" in reply
    assert chat.row()["confirmed_at"] and chat.row(second)["confirmed_at"]
    assert chat.row()["failed_attempts"] == 0


def test_a_confirmed_number_is_not_a_wrong_attempt(chat: Chat) -> None:
    second = chat.query("select shipment_id from shipments where driver_id<>? order by shipment_id limit 1", DRIVER)[0][0]
    conn = open_connection(path=chat.path)
    conn.execute("update shipments set driver_id=?, order_reference='ORD-261127-002' where shipment_id=?", (DRIVER, second))
    conn.commit()
    conn.close()
    chat.wait_for(second)
    assert chat.send("my order is " + NUMBER).startswith("Tell me more.")
    assert chat.row(second)["failed_attempts"] == 0


def test_a_cancelled_waiting_shipment_is_ignored(chat: Chat) -> None:
    chat.wait_for()
    conn = open_connection(path=chat.path)
    conn.execute("update shipments set current_status='CANCELLED' where shipment_id=?", (SHIP,))
    conn.commit()
    conn.close()
    assert chat.send("my order is " + NUMBER) == "Tell me more." and chat.row()["confirmed_at"] is None


def test_the_reply_follows_the_drivers_language(chat: Chat) -> None:
    chat.wait_for()
    assert "कन्फर्म" in chat.send("मेरा ऑर्डर ORD-261127-001 है")


def test_the_language_question_also_asks_for_the_number(chat: Chat) -> None:
    chat.wait_for()
    reply = chat.send("261127001")
    assert "English or Hindi" in reply and reply.endswith(say("order_waiting", "en")) and chat.model.calls == []


def test_many_numbers_in_one_message_are_many_attempts(chat: Chat) -> None:
    chat.wait_for()
    guesses = ", ".join(f"ORD261127{n:03d}" for n in range(100))
    assert chat.send(guesses) == say("order_locked", "en")
    assert chat.row()["confirmed_at"] is None and chat.row()["locked_at"] is not None


def test_wrong_numbers_add_up_across_messages(chat: Chat) -> None:
    chat.wait_for()
    assert chat.send("ORD-999999-001 ORD-999999-002, ORD-999999-003") == say("order_mismatch", "en")
    assert chat.row()["failed_attempts"] == 3
    assert chat.send("ORD-999999-004, ORD-999999-005") == say("order_locked", "en")


def test_after_the_lock_the_driver_is_sent_to_the_warehouse(chat: Chat) -> None:
    chat.wait_for()
    for _ in range(5):
        chat.send("ORD-999999-999")
    reply = chat.send("my truck broke down")
    assert reply.endswith(say("order_locked", "en")) and say("order_waiting", "en") not in reply
