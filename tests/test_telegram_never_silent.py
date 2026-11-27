"""A linked driver is answered, or the answer is queued and retried (UX audit 2026-11-27: UX-07)."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import json
from typing import Any

import pytest

from setuhaul.auth import telegram
from setuhaul.db import open_connection
from setuhaul.language import LANGS, T
from setuhaul.rate_limit import RateLimits
from tests.test_telegram_channel import bind, env, post, text_update  # noqa: F401 - env is a fixture


def _other(field: str, value: Any) -> dict[str, Any]:
    # An update carrying something that is not typed text.
    update = text_update("x")
    del update["message"]["text"]
    update["message"][field] = value
    return update


@pytest.mark.parametrize("update", [("photo", [{"file_id": "f"}]), ("voice", {"file_id": "v"}),
                                    ("location", {"latitude": 1.0, "longitude": 2.0}), ("sticker", {"file_id": "s"}), ("text", "   ")])
def test_unreadable_content_gets_an_answer(env, update: tuple[str, Any]) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    _, client, calls, fake, *_ = env
    bind(client)
    fake.sent.clear()
    assert post(client, _other(*update)).status_code == 200
    assert [text for _, text, _ in fake.sent] == [T["unsupported_content"]["en"]] and calls.n == 0


def test_unreadable_content_is_answered_in_the_drivers_language(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    _, client, _, fake, *_ = env
    bind(client)
    post(client, text_update("meri gaadi 60 minute late hai"))
    fake.sent.clear()
    post(client, _other("photo", [{"file_id": "f"}]))
    assert fake.sent[0][1] == T["unsupported_content"]["hinglish"]


def test_rate_limited_driver_is_told_to_wait(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    deps, client, _, fake, *_ = env
    bind(client)
    deps.rate_limits = RateLimits(1, 100)
    post(client, text_update("my truck is 60 minutes late"))
    fake.sent.clear()
    post(client, text_update("hello"))
    assert len(fake.sent) == 1 and "too quickly" in fake.sent[0][1]


def test_new_wording_exists_in_every_language() -> None:
    for key in ("unsupported_content", "slow_down", "not_processed"):
        assert set(T[key]) == set(LANGS)


class _Down(telegram.FakeTelegramClient):
    def send(self, chat_id: int, text: str, request_contact_button: bool = False) -> None:
        raise telegram.TelegramSendError("sendMessage failed: HTTP 502")


def _rows(path: Any, sql: str) -> list[Any]:
    conn = open_connection(path=path, readonly=True)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_failed_reply_is_queued_for_the_retrying_sender(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    deps, client, _, _, _, path = env
    bind(client)
    deps.telegram = _Down()
    assert post(client, text_update("my truck is 60 minutes late")).status_code == 200
    queued = _rows(path, "select payload_json, status from notification_outbox where recipient_type='DRIVER' and recipient_ref='DRV006'")
    assert len(queued) == 1 and queued[0]["status"] == "PENDING"
    assert json.loads(queued[0]["payload_json"])["text"]


def test_failed_reply_with_nothing_to_queue_on_is_recorded(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    deps, client, _, _, _, path = env
    bind(client)
    conn = open_connection(path=path)
    conn.execute("update shipments set current_status='COMPLETED' where driver_id='DRV006'")
    conn.commit()
    conn.close()
    deps.telegram = _Down()
    post(client, _other("photo", [{"file_id": "f"}]))
    assert len(_rows(path, "select 1 from notification_outbox where recipient_type='DRIVER' and recipient_ref='DRV006'")) == 1
