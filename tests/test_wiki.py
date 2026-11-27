"""The wiki page: how to use the application, readable by every signed-in role and linked from every screen."""

from __future__ import annotations

import html
import re
from pathlib import Path

import pytest

from setuhaul.auth import telegram, wiki_pages
from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from setuhaul.language import say
from tests.auth_helpers import JAIPUR, World, add_user, build_world

KEYS = ["start", "warehouse", "admin", "gate", "drivers"]
ERRORS = "If you see an error"


@pytest.fixture()
def world(tmp_path: Path, clock: FakeClock) -> World:
    made = build_world(tmp_path, clock)
    conn = open_connection(path=made.path)
    try:
        made.users["gate"] = add_user(conn, clock, "gate@x.com", "SECURITY", (JAIPUR,))
    finally:
        conn.close()
    return made


def header(text: str) -> str:
    return text.split("</header>", 1)[0]


@pytest.mark.parametrize("who", ["admin", "wh_a", "gate"])
def test_every_signed_in_role_sees_every_tab(world: World, who: str) -> None:
    r = world.client(who).get("/wiki")
    assert r.status_code == 200
    assert re.findall(r'data-tab="(\w+)"', r.text) == KEYS
    assert re.findall(r'<section id="\w+" data-panel="(\w+)"', r.text) == KEYS
    assert "/app/assets/tabs.js" in r.text and "<style" not in r.text and " style=" not in r.text


def test_anonymous_goes_to_sign_in(world: World) -> None:
    r = world.client().get("/wiki")
    assert r.status_code == 303 and r.headers["location"] == "/"


@pytest.mark.parametrize("who,shown", [("admin", {"/warehouse", "/admin", "/gate", "/wiki"}),
                                       ("wh_a", {"/warehouse", "/wiki"}), ("gate", {"/gate", "/wiki"})])
def test_menu_shows_only_screens_the_role_can_open(world: World, who: str, shown: set[str]) -> None:
    assert set(re.findall(r'href="(/\w+)"', header(world.client(who).get("/wiki").text))) == shown


@pytest.mark.parametrize("who,path", [("wh_a", "/warehouse"), ("admin", "/admin"), ("gate", "/gate")])
def test_every_screen_links_to_the_wiki(world: World, who: str, path: str) -> None:
    assert 'href="/wiki"' in header(world.client(who).get(path).text)


def test_named_buttons_exist_on_the_screens(world: World) -> None:
    client = world.client("admin")
    wiki = html.unescape(client.get("/wiki").text)
    screens = {
        f"/warehouse?facility={JAIPUR}": ("Check bookings", "Block a dock", "Reopen", "Add driver", "Delete", "Open availability", "Create shipment",
                       "Claim"),
        "/admin": ("Invite", "Audit log", "Deleted drivers", "Pending drivers", "Onboarded drivers", "Load from LangSmith"),
        "/gate": ("Check pass", "Check out by pass", "Trucks inside"),
    }
    for path, words in screens.items():
        screen = html.unescape(client.get(path).text)
        for word in words:
            assert word in screen, (path, word)
            assert word in wiki, ("wiki", word)


def test_each_screen_tab_explains_errors() -> None:
    for key, _, tasks in wiki_pages.TOPICS:
        if key != "start":
            assert ERRORS in [heading for heading, _ in tasks], key


def driver_tab() -> dict[str, tuple[str, ...]]:
    return dict(next(tasks for key, _, tasks in wiki_pages.TOPICS if key == "drivers"))


def test_driver_tab_walks_through_registering_and_messaging() -> None:
    tasks = driver_tab()
    assert list(tasks)[:3] == ["Before the driver starts (office staff)", "Register on the phone (driver)",
                               "Send a message (driver)"]
    register = " ".join(tasks["Register on the phone (driver)"])
    # The words the driver will really see, so the instructions cannot drift from the bot.
    assert "Share my phone number" in register and telegram.BOUND_REPLY in register
    office = " ".join(tasks["Before the driver starts (office staff)"])
    assert "Add driver" in office and "Approve" in office and "Test bot" in office
    assert "Text to send your driver" in tasks
    assert telegram.NEUTRAL_REFUSAL.split(".")[0] in " ".join(tasks[ERRORS])
    for key in ("unsupported_content", "slow_down"):
        assert say(key, "en", seconds="60").split(".")[0] in " ".join(tasks[ERRORS]), key


def test_warehouse_instructions_describe_the_board_and_operations_panel() -> None:
    text = " ".join(step for key, _, tasks in wiki_pages.TOPICS for _, steps in tasks for step in steps)
    assert "Messages tab" not in text and "small ×" not in text and "under Messages" not in text
    assert "Free this slot" in text and "Operations" in text
