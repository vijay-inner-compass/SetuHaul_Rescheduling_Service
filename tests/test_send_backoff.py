"""A failed Telegram send is retried after growing waits, without letting later messages overtake it."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from setuhaul.auth import telegram
from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection
from setuhaul.jobs.send_driver_notifications import DriverNoticeRunner
from tests.test_telegram_delivery import _bind, _queue, _row


class _Outage:
    """Fails while ``down``; records what was tried and what got through."""

    def __init__(self, failing_chat: int | None = None) -> None:
        self.down = True
        self.failing_chat = failing_chat
        self.tried: list[str] = []
        self.sent: list[str] = []

    def send(self, chat_id: int, text: str, request_contact_button: bool = False) -> None:
        self.tried.append(text)
        if self.down and self.failing_chat in (None, chat_id):
            raise telegram.TelegramSendError("sendMessage failed: ConnectError")
        self.sent.append(text)


def _setup(tmp_path: Path, clock: FakeClock, client: _Outage):  # type: ignore[no-untyped-def]
    path = make_working_db(tmp_path / "b.sqlite3")
    conn = open_connection(path=path)
    _bind(conn, clock, 111, "9000010006")
    return conn, DriverNoticeRunner(clock, client, lambda: open_connection(path=path))


def _pass_after(runner: DriverNoticeRunner, clock: FakeClock, seconds: int) -> None:
    clock.advance(timedelta(seconds=seconds))
    assert runner.run_once() is not None


def test_a_failed_send_waits_thirty_seconds_before_trying_again(tmp_path: Path, clock: FakeClock) -> None:
    client = _Outage()
    conn, runner = _setup(tmp_path, clock, client)
    _queue(conn, "DRV006", "k1", {"text": "one"})
    _pass_after(runner, clock, 0)
    _pass_after(runner, clock, 5)
    _pass_after(runner, clock, 24)
    assert len(client.tried) == 1
    _pass_after(runner, clock, 1)
    assert len(client.tried) == 2


def test_the_waits_grow_and_the_fifth_failure_is_final(tmp_path: Path, clock: FakeClock) -> None:
    client = _Outage()
    conn, runner = _setup(tmp_path, clock, client)
    oid = _queue(conn, "DRV006", "k1", {"text": "one"})
    _pass_after(runner, clock, 0)
    for number, wait in enumerate((30, 60, 180, 300), start=2):
        _pass_after(runner, clock, wait - 1)
        assert len(client.tried) == number - 1
        _pass_after(runner, clock, 1)
        assert len(client.tried) == number
    assert _row(conn, oid)[0] == "PERMANENT_FAILURE"
    _pass_after(runner, clock, 600)
    assert len(client.tried) == 5


def test_a_message_gets_through_after_a_five_minute_outage(tmp_path: Path, clock: FakeClock) -> None:
    client = _Outage()
    conn, runner = _setup(tmp_path, clock, client)
    oid = _queue(conn, "DRV006", "k1", {"text": "one"}, when=clock.now())
    # A pass every five seconds, as the server does; the connection returns after five minutes.
    for tick in range(121):
        client.down = tick * 5 < 300
        _pass_after(runner, clock, 5 if tick else 0)
    assert client.sent == ["one"] and _row(conn, oid)[0] == "SENT"


def test_later_messages_for_the_driver_wait_their_turn(tmp_path: Path, clock: FakeClock) -> None:
    client = _Outage()
    conn, runner = _setup(tmp_path, clock, client)
    _queue(conn, "DRV006", "k1", {"text": "one"})
    _pass_after(runner, clock, 0)
    client.down = False
    _queue(conn, "DRV006", "k2", {"text": "two"})
    _pass_after(runner, clock, 5)
    assert client.sent == []
    _pass_after(runner, clock, 25)
    assert client.sent == ["one", "two"]


def test_one_drivers_failure_does_not_delay_another(tmp_path: Path, clock: FakeClock) -> None:
    client = _Outage(failing_chat=1111)
    conn, runner = _setup(tmp_path, clock, client)
    _bind(conn, clock, 222, "9000010007")
    _queue(conn, "DRV006", "k1", {"text": "stuck"})
    _queue(conn, "DRV007", "k2", {"text": "fine"})
    _pass_after(runner, clock, 0)
    assert client.sent == ["fine"]


def test_a_message_given_up_on_no_longer_holds_the_next(tmp_path: Path, clock: FakeClock) -> None:
    client = _Outage()
    conn, runner = _setup(tmp_path, clock, client)
    first = _queue(conn, "DRV006", "k1", {"text": "one"}, when=clock.now() - timedelta(minutes=29, seconds=50))
    second = _queue(conn, "DRV006", "k2", {"text": "two"}, when=clock.now())
    _pass_after(runner, clock, 0)
    client.down = False
    # The first passes the 30-minute limit while waiting.
    _pass_after(runner, clock, 15)
    assert client.sent == ["two"]
    assert _row(conn, first) == ("PERMANENT_FAILURE", "TOO_OLD") and _row(conn, second)[0] == "SENT"
