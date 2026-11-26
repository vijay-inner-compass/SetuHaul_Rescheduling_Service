"""The Telegram bot follows the saved settings without a restart."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from setuhaul import app_settings
from setuhaul.api import create_app
from setuhaul.auth import telegram
from setuhaul.db import open_connection
from setuhaul.runtime_settings import SettingsSource
from tests.runtime_helpers import BASE, save
from tests.test_telegram_channel import contact_update, env, post, text_update  # noqa: F401 - env is a fixture
from tests.test_telegram_delivery import _queue

SECRET = "saved-secret-0123456789"
SAVED = {"SETUHAUL_TELEGRAM_BOT_TOKEN": "111:saved", "SETUHAUL_TELEGRAM_WEBHOOK_SECRET": SECRET}
HDR = {"X-Telegram-Bot-Api-Secret-Token": SECRET}


class Bots:
    """One fake client per token asked for."""

    def __init__(self) -> None:
        self.made: dict[str, telegram.FakeTelegramClient] = {}

    def __call__(self, token: str) -> telegram.FakeTelegramClient:
        return self.made.setdefault(token, telegram.FakeTelegramClient())


def _live(deps, bots: Bots, base: dict[str, str] = BASE) -> None:  # type: ignore[no-untyped-def]
    # As the service starts with no bot in its environment.
    deps.telegram_secret, deps.telegram = b"", None
    deps.settings = SettingsSource({**base, "SETUHAUL_MODEL_PROVIDER": ""}, telegram_factory=bots)


def test_a_bot_saved_after_start_up_receives_the_webhook(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    deps, client, *_, path = env
    bots = Bots()
    _live(deps, bots)
    r = post(client, text_update("hi"), headers=HDR)
    assert r.status_code == 503 and r.json()["reason_code"] == "TELEGRAM_NOT_CONFIGURED"
    save(path, SAVED)
    assert post(client, text_update("hi"), headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}).status_code == 401
    assert post(client, contact_update(), headers=HDR).status_code == 200
    assert [m[1] for m in bots.made["111:saved"].sent] == [telegram.BOUND_REPLY]


def test_a_short_or_unreadable_pair_leaves_telegram_off(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    deps, client, *_, path = env
    save(path, {**SAVED, "SETUHAUL_TELEGRAM_WEBHOOK_SECRET": "short"})
    _live(deps, Bots())
    assert post(client, text_update("hi"), headers={"X-Telegram-Bot-Api-Secret-Token": "short"}).status_code == 503
    save(path, SAVED)
    _live(deps, Bots(), {**BASE, "SETUHAUL_SETTINGS_KEY": ""})
    assert post(client, text_update("hi"), headers=HDR).status_code == 503


def _status(path, oid: int) -> tuple[str, int]:  # type: ignore[no-untyped-def]
    conn = open_connection(path=path)
    row = conn.execute("select status, attempts from notification_outbox where outbox_id=?", (oid,)).fetchone()
    conn.close()
    return row[0], row[1]


def _wait_for(check) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + 5
    while not check():
        assert time.monotonic() < deadline
        time.sleep(0.02)


def test_queued_notices_wait_for_a_bot_then_use_it(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    deps, client, *_, path = env
    assert post(client, contact_update()).status_code == 200  # bind the driver
    conn = open_connection(path=path)
    oid = _queue(conn, "DRV006", "k1")
    conn.close()
    bots = Bots()
    _live(deps, bots)
    deps.notice_interval_s = 0.01
    before = _status(path, oid)
    # Started with no bot at all: the sender still runs.
    with TestClient(create_app(deps)):
        time.sleep(0.2)
        assert _status(path, oid) == before and not bots.made
        save(path, SAVED)
        _wait_for(lambda: _status(path, oid)[0] == "SENT")
    assert [m[1] for m in bots.made["111:saved"].sent] == ["Slot moved to 10:00"]


def test_reset_stops_sending_without_failing_notices(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    deps, client, *_, path = env
    assert post(client, contact_update()).status_code == 200
    save(path, SAVED)
    bots = Bots()
    _live(deps, bots)
    conn = open_connection(path=path)
    app_settings.reset(conn, SAVED)
    conn.commit()
    oid = _queue(conn, "DRV006", "k1")
    conn.close()
    before = _status(path, oid)
    deps.notice_interval_s = 0.01
    with TestClient(create_app(deps)):
        time.sleep(0.2)
    assert _status(path, oid) == before and not bots.made
