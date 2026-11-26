"""Stale boards, the focus outline and narrow screens (UX audit 2026-11-27: UX-04, UX-11, UX-17)."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from setuhaul.clock import FakeClock
from tests.auth_helpers import World, build_world

SRC = Path(__file__).resolve().parents[1] / "src" / "setuhaul"


@pytest.fixture()
def world(tmp_path: Path, clock: FakeClock) -> World:
    return build_world(tmp_path, clock)


def _page(world: World, who: str, path: str) -> str:
    r = world.client(who).get(path)
    assert r.status_code == 200
    return str(r.text)


def _asset(name: str) -> str:
    return (SRC / "auth" / "assets" / name).read_text(encoding="utf-8")


def _contrast(first: str, second: str) -> float:
    def light(colour: str) -> float:
        raw = colour.lstrip("#")
        raw = "".join(c * 2 for c in raw) if len(raw) == 3 else raw
        parts = [int(raw[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        r, g, b = (v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in parts)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    high, low = sorted((light(first), light(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


# ---- stale screens -----------------------------------------------------------------------------------------
def test_warehouse_page_has_a_place_to_say_how_fresh_it_is(world: World) -> None:
    page = _page(world, "wh_a", "/warehouse")
    # The state is announced when it changes; the ticking time is not, or a screen reader would read it every 3 seconds.
    assert re.search(r'<p class="live" data-live><span data-live-state role="status">[^<]*</span> <span data-live-time></span></p>', page)


def test_everything_on_the_board_is_refreshed_together(world: World) -> None:
    page = _page(world, "wh_a", "/warehouse")
    script = _asset("warehouse.js")
    live = re.search(r"var LIVE = \[(.*?)\];", script, re.DOTALL)
    assert live
    for selector, mark in (("#board .asof", 'class="legend asof"'), ("#board .waiting", 'class="waiting"'),
                           ("#board .bays", 'class="bays"'), ("#board .scroll", 'class="scroll"')):
        assert selector in live.group(1) and mark in page


@pytest.mark.parametrize("panel", ["projects", "shipments", "drivers"])
def test_tabs_that_do_not_refresh_say_so(world: World, panel: str) -> None:
    page = _page(world, "wh_a", "/warehouse")
    section = page.split(f'data-panel="{panel}"', 2)[-1].split("</section>", 1)[0]
    assert "does not update by itself" in section


# ---- focus outline -----------------------------------------------------------------------------------------
def test_focus_outline_is_visible_on_light_and_dark_surfaces() -> None:
    css = _asset("app.css")
    light = re.search(r":root \{ --focus: (#[0-9a-f]{3,6}); \}", css)
    dark = re.search(r"^header \{ --focus: (#[0-9a-f]{3,6}); \}", css, re.MULTILINE)
    assert light and dark
    for surface in ("#ffffff", "#f6f7f9", "#f8f9fb"):
        assert _contrast(light.group(1), surface) >= 3
    assert _contrast(dark.group(1), "#17324d") >= 3
    assert re.search(r"^:focus-visible \{ outline: 3px solid var\(--focus\); outline-offset: 2px; \}", css, re.MULTILINE)
    # No other rule paints the outline in its own colour.
    outlines = re.findall(r"outline: [^;}]*?(#[0-9a-f]{3,6})", css)
    assert not outlines, outlines


def test_demo_console_focus_outline_is_visible_on_its_dark_bar() -> None:
    page = (SRC / "demo" / "index.html").read_text(encoding="utf-8")
    rule = re.search(r"\.appbar :focus-visible\{outline-color:(#[0-9a-f]{6})\}", page)
    assert rule and _contrast(rule.group(1), "#12355b") >= 3


# ---- narrow screens ----------------------------------------------------------------------------------------
class _Tables(HTMLParser):
    """Counts tables that have no scrolling box around them."""

    def __init__(self) -> None:
        super().__init__()
        self.divs: list[bool] = []
        self.bare = 0
        self.total = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "div":
            self.divs.append("scroll" in (dict(attrs).get("class") or "").split())
        elif tag == "table":
            self.total += 1
            self.bare += not any(self.divs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self.divs:
            self.divs.pop()


@pytest.mark.parametrize(("who", "path"), [("admin", "/admin"), ("admin", "/warehouse"), ("wh_a", "/warehouse")])
def test_every_table_scrolls_inside_its_own_box(world: World, who: str, path: str) -> None:
    found = _Tables()
    found.feed(_page(world, who, path))
    assert found.total > 0 and found.bare == 0
    assert re.search(r"^\.scroll \{ overflow-x: auto; \}", _asset("app.css"), re.MULTILINE)
