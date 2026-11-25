"""Driver chat privacy: a warehouse authority sees only messages sent while the driver is allocated to one of their own live projects."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from setuhaul.auth import projects
from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from tests.auth_helpers import GURUGRAM, JAIPUR, World, build_world

# DRV006 is wh_b's project driver; its seeded message was sent before anyone in Jaipur had it.
OLD_TEXT = f"hello from DRV006 for {GURUGRAM}"
THREAD = f"THR-T-{GURUGRAM}"


@pytest.fixture()
def world(tmp_path: Path, clock: FakeClock) -> World:
    return build_world(tmp_path, clock)


def _project_of(world: World, who: str) -> str:
    conn = open_connection(path=world.path)
    try:
        return str(conn.execute("select project_id from projects where created_by=? order by project_id",
                                (world.users[who].user_id,)).fetchone()[0])
    finally:
        conn.close()


def _allocate(world: World, who: str, project_id: str, driver_id: str = "DRV006") -> None:
    conn = open_connection(path=world.path)
    try:
        projects.allocate(conn, actor=world.users[who], project_id=project_id, driver_id=driver_id, clock=world.clock)
    finally:
        conn.close()


def _release(world: World, who: str, project_id: str, driver_id: str = "DRV006") -> None:
    conn = open_connection(path=world.path)
    try:
        projects.release(conn, actor=world.users[who], project_id=project_id, driver_id=driver_id, clock=world.clock)
    finally:
        conn.close()


def _say(world: World, text: str, ts: str | None = None) -> None:
    # A driver message in DRV006's thread, stamped now unless a stored form is given.
    conn = open_connection(path=world.path)
    try:
        n = conn.execute("select count(*) from chat_messages").fetchone()[0]
        conn.execute("insert into chat_messages(chat_message_id, thread_id, sender_type, message_text, message_ts) values (?,?,'DRIVER',?,?)",
                     (f"MSG-W-{n}", THREAD, text, ts or world.clock.now().isoformat()))
        conn.commit()
    finally:
        conn.close()


def _texts(world: World, who: str, driver_id: str = "DRV006") -> list[str]:
    got = {d["driver_id"]: d for d in world.client(who).get("/v1/warehouse/driver-chats").json()["drivers"]}
    return [m["text"] for m in got[driver_id]["messages"]] if driver_id in got else []


def test_history_from_before_the_allocation_is_hidden(world: World) -> None:
    world.clock.advance(timedelta(hours=1))
    # wh_a allocates wh_b's driver to their own project: the earlier chat (another facility's load) stays hidden.
    _allocate(world, "wh_a", _project_of(world, "wh_a"))
    assert OLD_TEXT not in _texts(world, "wh_a")
    world.clock.advance(timedelta(minutes=5))
    _say(world, "new while in Jaipur")
    assert _texts(world, "wh_a") == ["new while in Jaipur"]
    # wh_b, whose project had the driver all along, still sees both.
    assert _texts(world, "wh_b") == [OLD_TEXT, "new while in Jaipur"]


def test_a_driver_with_no_message_in_the_window_is_not_listed(world: World) -> None:
    world.clock.advance(timedelta(hours=1))
    _allocate(world, "wh_a", _project_of(world, "wh_a"))
    got = {d["driver_id"] for d in world.client("wh_a").get("/v1/warehouse/driver-chats").json()["drivers"]}
    assert got == {"DRV001"}


def test_the_gap_between_release_and_reallocation_stays_hidden(world: World) -> None:
    pid = _project_of(world, "wh_a")
    world.clock.advance(timedelta(hours=1))
    _allocate(world, "wh_a", pid)
    world.clock.advance(timedelta(minutes=1))
    _say(world, "during the first allocation")
    world.clock.advance(timedelta(minutes=1))
    _release(world, "wh_a", pid)
    # Released: nothing at all.
    assert _texts(world, "wh_a") == []
    world.clock.advance(timedelta(minutes=1))
    _say(world, "in the gap")
    world.clock.advance(timedelta(minutes=1))
    _allocate(world, "wh_a", pid)
    world.clock.advance(timedelta(minutes=1))
    _say(world, "after re-allocation")
    texts = _texts(world, "wh_a")
    assert "in the gap" not in texts and "after re-allocation" in texts


def test_a_project_past_its_end_date_shows_nothing(world: World) -> None:
    conn = open_connection(path=world.path)
    try:
        today = world.clock.now().date()
        pid = projects.create(conn, actor=world.users["wh_a"], customer_name="Short", contract_ref="SHORT-1", facility_id=JAIPUR,
                              starts_on=today, ends_on=today, clock=world.clock)
    finally:
        conn.close()
    world.clock.advance(timedelta(minutes=1))
    _allocate(world, "wh_a", pid)
    world.clock.advance(timedelta(minutes=1))
    _say(world, "on the last day")
    # The last day still counts.
    assert _texts(world, "wh_a") == ["on the last day"]
    world.clock.advance(timedelta(days=1))
    assert _texts(world, "wh_a") == []
    conn = open_connection(path=world.path)
    try:
        # visible_driver_ids applies the same end-date rule (DRV001 is on an open-ended project).
        assert projects.visible_driver_ids(conn, world.users["wh_a"], world.clock.now().date()) == {"DRV001"}
    finally:
        conn.close()
    assert "DRV006" not in {d["driver_id"] for d in world.client("wh_a").get("/v1/warehouse/drivers").json()["drivers"]}


def test_timestamps_compare_as_instants_not_strings(world: World) -> None:
    world.clock.advance(timedelta(hours=1))
    _allocate(world, "wh_a", _project_of(world, "wh_a"))
    conn = open_connection(path=world.path)
    try:
        # The allocation instant carries microseconds; the messages below use other stored forms.
        conn.execute("update project_drivers set allocated_at='2026-08-04T10:35:00.500000+05:30' where driver_id='DRV006'")
        conn.commit()
    finally:
        conn.close()
    _say(world, "utc after", "2026-08-04T05:05:01Z")
    _say(world, "half a second early", "2026-08-04T10:35:00+05:30")
    _say(world, "utc before", "2026-08-04T05:04:59Z")
    _say(world, "not a timestamp", "yesterday")
    assert _texts(world, "wh_a") == ["utc after"]


def test_admin_still_sees_the_whole_history(world: World) -> None:
    world.clock.advance(timedelta(hours=1))
    _allocate(world, "wh_a", _project_of(world, "wh_a"))
    got = {d["driver_id"]: d for d in world.client("admin").get("/v1/warehouse/driver-chats").json()["drivers"]}
    assert OLD_TEXT in [m["text"] for m in got["DRV006"]["messages"]]


def test_the_warehouse_page_uses_the_same_window(world: World) -> None:
    world.clock.advance(timedelta(hours=1))
    _allocate(world, "wh_a", _project_of(world, "wh_a"))
    world.clock.advance(timedelta(minutes=1))
    _say(world, "fresh page message")
    page = world.client("wh_a").get("/warehouse").text
    assert "fresh page message" in page and OLD_TEXT not in page
