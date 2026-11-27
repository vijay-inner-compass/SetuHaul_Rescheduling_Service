"""The driver confirms an assigned shipment by sending its pass back; the app checks it and never keeps the code."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import fakeredis
import pytest
from fastapi.testclient import TestClient

from setuhaul import passes
from setuhaul.api import AppDeps, create_app
from setuhaul.auth import telegram
from setuhaul.clock import FakeClock
from setuhaul.db import immediate, make_working_db, open_connection
from setuhaul.dock_pass import new_code, normalise
from setuhaul.identity import provision_identities
from setuhaul.language import say
from setuhaul.model_gateway import ModelMessage, ModelPolicy, ModelResponse
from setuhaul.rate_limit import RateLimits
from tests.helpers import at, seed_everything_known
from tests.shipment_helpers import mark_unconfirmed
from tests.test_telegram_channel import SECRET, bind, post, text_update

SHIP, DRIVER = "SHP1006", "DRV006"


class Recorder:
    """A model stand-in that remembers every prompt it was given."""

    def __init__(self) -> None:
        self.calls: list[Sequence[ModelMessage]] = []

    def complete(self, messages: Sequence[ModelMessage], tools: Sequence[dict[str, Any]], policy: ModelPolicy) -> ModelResponse:
        self.calls.append(tuple(messages))
        return ModelResponse(text="Tell me more.")


class Chat:
    def __init__(self, tmp_path: Path) -> None:
        self.path = make_working_db(tmp_path / "pc.sqlite3")
        conn = open_connection(path=self.path)
        seed_everything_known(conn)
        provision_identities(conn)
        conn.close()
        self.model = Recorder()
        self.bot = telegram.FakeTelegramClient()
        self.redis = fakeredis.FakeRedis(decode_responses=True)
        self.clock = FakeClock(at("09:35"))
        deps = AppDeps(db_path=self.path, redis=self.redis, webhook_secret=b"w", coordinator_tokens={},
                       fallback_path=tmp_path / "f.jsonl", policy=ModelPolicy(provider="stub", model_id="stub"),
                       gateway_factory=lambda: self.model, clock=self.clock, rate_limits=RateLimits(50, 200),
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
            rows = conn.execute(sql, args).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    def issue(self, shipment_id: str = SHIP, wait: bool = True) -> str:
        """A pass for a supplied shipment made to look app-created, waiting for the driver."""
        conn = open_connection(path=self.path)
        try:
            if wait:
                self.query("update shipments set driver_id=? where shipment_id=?", DRIVER, shipment_id)
                mark_unconfirmed(conn, shipment_id)
            with immediate(conn):
                return passes.issue(conn, shipment_id, self.clock, None)
        finally:
            conn.close()

    def second(self) -> str:
        return str(self.query("select shipment_id from appointments where is_current=1 and appointment_status='CONFIRMED' "
                              "and shipment_id<>? order by shipment_id limit 1", SHIP)[0][0])

    def row(self, shipment_id: str = SHIP) -> sqlite3.Row:
        return self.query("select * from shipment_assignments where shipment_id=?", shipment_id)[0]

    def prompt(self) -> str:
        return self.model.calls[-1][0].content

    def stored(self) -> str:
        conn = open_connection(path=self.path)
        try:
            names = [r[0] for r in conn.execute("select name from sqlite_master where type='table'")]
            text = " ".join(str(tuple(row)) for n in names for row in conn.execute(f'select * from "{n}"'))
        finally:
            conn.close()
        for key in self.redis.keys("*"):
            if self.redis.type(key) == "string":
                text += " " + str(self.redis.get(key))
        prompts = " ".join(m.content for call in self.model.calls for m in call)
        return normalise(text + " " + prompts)


@pytest.fixture()
def chat(tmp_path: Path) -> Chat:
    return Chat(tmp_path)


def test_the_right_pass_confirms_without_calling_the_model(chat: Chat) -> None:
    code = chat.issue()
    reply = chat.send(code.lower().replace("-", " "))
    assert "SetuHaul" in reply and "ORD-" in reply and chat.model.calls == []
    assert chat.row()["confirmed_at"] is not None
    saved = chat.query("select m.sender_type, m.message_text from chat_messages m join chat_threads t using(thread_id) "
                       "where t.driver_id=? order by m.rowid desc limit 2", DRIVER)
    assert [(r["sender_type"], r["message_text"]) for r in reversed(saved)] == [("DRIVER", "DAP-[hidden]"), ("AGENT", reply)]


@pytest.mark.parametrize("shape", [lambda c: c, lambda c: normalise(c)[3:], lambda c: c.replace("-", " "), lambda c: c[:-1]])
def test_the_code_is_never_kept(chat: Chat, shape) -> None:  # type: ignore[no-untyped-def]
    code = chat.issue()
    chat.send("hello, here it is " + shape(code))
    chat.send("and what happens now with my delivery")
    assert normalise(code)[3:19] not in chat.stored()


def test_the_code_is_not_kept_when_nothing_is_waiting(chat: Chat) -> None:
    code = new_code()
    chat.send("please look at " + code)
    assert normalise(code)[3:19] not in chat.stored()


def test_an_order_number_alone_does_nothing(chat: Chat) -> None:
    chat.issue()
    number = chat.query("select order_reference from shipments where shipment_id=?", SHIP)[0][0]
    reply = chat.send("my order is " + number)
    assert reply.endswith(say("pass_waiting", "en"))
    assert chat.row()["confirmed_at"] is None and chat.row()["failed_attempts"] == 0
    assert SHIP not in chat.prompt()


def test_a_superseded_or_revoked_pass_is_wrong(chat: Chat) -> None:
    old = chat.issue()
    new = chat.issue(wait=False)
    assert chat.send(old) == say("pass_mismatch", "en") and chat.row()["failed_attempts"] == 1
    chat.query("update dock_access_authorizations set status='REVOKED' where status='ISSUED'")
    assert chat.send(new) == say("pass_mismatch", "en") and chat.row()["confirmed_at"] is None


def test_fifth_wrong_pass_locks_and_opens_one_case(chat: Chat) -> None:
    right = chat.issue()
    for _ in range(4):
        assert chat.send(new_code()) == say("pass_mismatch", "en")
    assert chat.send(new_code()) == say("pass_locked", "en")
    cases = chat.query("select shipment_id from escalation_cases where reason_code='PASS_CONFIRMATION_LOCKED'")
    assert [c["shipment_id"] for c in cases] == [SHIP]
    assert chat.send(right) == say("pass_locked", "en")
    assert chat.row()["confirmed_at"] is None and chat.row()["failed_attempts"] == 5
    reply = chat.send("my truck broke down")
    assert reply.endswith(say("pass_locked", "en")) and say("pass_waiting", "en") not in reply


def test_many_codes_in_one_message_are_many_attempts(chat: Chat) -> None:
    right = chat.issue()
    guesses = " , ".join(new_code() for _ in range(40))
    assert chat.send(f"{guesses} , {right}") == say("pass_locked", "en")
    assert chat.row()["confirmed_at"] is None and chat.row()["locked_at"] is not None


def test_a_replayed_message_counts_once(chat: Chat) -> None:
    chat.issue()
    wrong = new_code()
    chat.send(wrong, update_id=7001)
    chat.send(wrong, update_id=7001)
    assert chat.row()["failed_attempts"] == 1


def test_one_message_confirms_every_pass_in_it(chat: Chat) -> None:
    other = chat.second()
    first, second = chat.issue(), chat.issue(other)
    chat.send(f"{first} and {second}")
    assert chat.row()["confirmed_at"] and chat.row(other)["confirmed_at"] and chat.row()["failed_attempts"] == 0


def test_a_confirmed_pass_repeated_is_not_a_wrong_attempt(chat: Chat) -> None:
    other = chat.second()
    first = chat.issue()
    chat.send(first)
    chat.issue(other)
    assert chat.send("here is my pass again " + first).startswith("Tell me more.")
    assert chat.row(other)["failed_attempts"] == 0


def test_no_waiting_shipment_changes_nothing(chat: Chat) -> None:
    reply = chat.send("hello, I have a question about my delivery")
    assert reply == "Tell me more." and "shipment_waiting_for_dock_access_pass" not in chat.prompt()


def test_the_reply_follows_the_drivers_language(chat: Chat) -> None:
    code = chat.issue()
    assert "स्वीकार" in chat.send("यह मेरा पास है " + code)


def test_the_language_question_also_asks_for_the_pass(chat: Chat) -> None:
    chat.issue()
    reply = chat.send("261127001")
    assert "English or Hindi" in reply and reply.endswith(say("pass_waiting", "en")) and chat.model.calls == []
