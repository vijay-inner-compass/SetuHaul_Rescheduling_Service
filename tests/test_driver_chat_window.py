"""Driver chat privacy: a warehouse authority sees the messages recorded for loads going to their facilities, nothing else."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from setuhaul import chat_loads
from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from tests.auth_helpers import GURUGRAM, JAIPUR, World, build_world

# DRV006's seeded thread, about a Gurugram load.
THREAD = f"THR-T-{GURUGRAM}"


@pytest.fixture()
def world(tmp_path: Path, clock: FakeClock) -> World:
    return build_world(tmp_path, clock)


def _say(world: World, text: str, load: str | None = "SHP1021", thread: str = THREAD, ts: str | None = None) -> None:
    # A driver message, recorded against `load` as the service would (None: not tied to a load).
    conn = open_connection(path=world.path)
    try:
        after = chat_loads.mark(conn)
        conn.execute("insert into chat_messages(chat_message_id, thread_id, sender_type, message_text, message_ts) values (?,?,'DRIVER',?,?)",
                     (f"MSG-W-{after}", thread, text, ts or world.clock.now().isoformat()))
        chat_loads.record(conn, thread, after, load)
        conn.commit()
    finally:
        conn.close()


def _texts(world: World, who: str, **params: int) -> list[str]:
    got = world.client(who).get("/v1/warehouse/driver-chats", params=params).json()["drivers"]
    return [m["text"] for d in got for m in d["messages"]]


def test_authority_sees_messages_for_their_facility_only(world: World) -> None:
    world.clock.advance(timedelta(minutes=1))
    # The same driver and thread, about two loads for two warehouses.
    _say(world, "about the Jaipur load", "SHP1006")
    _say(world, "about the Gurugram load", "SHP1021")
    jaipur, gurugram = _texts(world, "wh_a"), _texts(world, "wh_b")
    assert "about the Jaipur load" in jaipur and "about the Gurugram load" not in jaipur
    assert "about the Gurugram load" in gurugram and "about the Jaipur load" not in gurugram


def test_a_driver_they_did_not_add_and_hold_no_project_for_is_visible(world: World) -> None:
    # wh_a has no project with DRV006; the load going to Jaipur is enough.
    _say(world, "hello from a stranger", "SHP1006")
    assert "hello from a stranger" in _texts(world, "wh_a")


def test_unrecorded_messages_are_admin_only(world: World) -> None:
    _say(world, "just hello", None)
    assert "just hello" not in _texts(world, "wh_a") and "just hello" not in _texts(world, "wh_b")
    assert "just hello" in _texts(world, "admin")


def test_no_facility_sees_nothing(world: World) -> None:
    _say(world, "private", "SHP1006")
    # An authority with no facility is refused outright, as before.
    got = world.client("wh_none").get("/v1/warehouse/driver-chats")
    assert got.status_code == 403 and "private" not in got.text


def test_limits_never_leak(world: World) -> None:
    _say(world, "gurugram only", "SHP1021")
    got = _texts(world, "wh_a", limit=100000, messages=100000)
    assert got and "gurugram only" not in got and f"hello from DRV006 for {GURUGRAM}" not in got


def test_messages_read_oldest_first_and_unreadable_times_are_hidden(world: World) -> None:
    _say(world, "second", "SHP1006", ts="2026-08-04T11:00:00+05:30")
    _say(world, "first", "SHP1006", ts="2026-08-04T04:30:00Z")
    _say(world, "no time", "SHP1006", ts="yesterday")
    texts = [t for t in _texts(world, "wh_a") if t in ("first", "second", "no time")]
    assert texts == ["first", "second"]


def test_the_warehouse_page_uses_the_same_rule(world: World) -> None:
    _say(world, "page message", "SHP1006")
    page = world.client("wh_a").get("/warehouse").text
    assert "page message" in page and f"hello from DRV006 for {GURUGRAM}" not in page
    assert "You see messages about loads going to your facilities." in page
    assert f"hello from DRV001 for {JAIPUR}" in page
