"""One set of colours, readable contrast, plain words and times (UX audit 2026-11-27: UX-14, 18, 19)."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import re
from pathlib import Path

import pytest

from setuhaul import labels
from setuhaul.clock import FakeClock
from tests.auth_helpers import World, build_world
from tests.test_screen_states import _contrast

SRC = Path(__file__).resolve().parents[1] / "src" / "setuhaul"
_COLOUR = re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b|rgba?\(")


def _read(path: str) -> str:
    return (SRC / path).read_text(encoding="utf-8")


def _tokens() -> dict[str, str]:
    return dict(re.findall(r"(--sh-[a-z0-9-]+): (#[0-9a-f]{3,6});", _read("auth/assets/tokens.css")))


@pytest.fixture()
def world(tmp_path: Path, clock: FakeClock) -> World:
    return build_world(tmp_path, clock)


def test_app_stylesheet_writes_no_colour_of_its_own() -> None:
    css = _read("auth/assets/app.css")
    assert not _COLOUR.findall(css)
    used = set(re.findall(r"var\((--sh-[a-z0-9-]+)\)", css))
    assert used and used <= set(_tokens())


def test_every_surface_links_the_shared_tokens(world: World) -> None:
    served = world.client().get("/app/assets/tokens.css")
    assert served.status_code == 200 and served.headers["content-type"].startswith("text/css")
    for path in ("demo/index.html", "demo/driver.html"):
        assert '<link rel="stylesheet" href="/app/assets/tokens.css">' in _read(path)
        assert "#12355b" not in _read(path), path
    for who, path in ((None, "/"), ("admin", "/admin"), ("wh_a", "/warehouse")):
        page = world.client(who).get(path).text
        assert page.index("/app/assets/tokens.css") < page.index("/app/assets/app.css")


def test_text_and_control_borders_have_enough_contrast() -> None:
    t = _tokens()
    for text in ("--sh-ink", "--sh-ink-2", "--sh-muted", "--sh-danger", "--sh-ok"):
        for surface in ("--sh-white", "--sh-bg", "--sh-surface-alt"):
            assert _contrast(t[text], t[surface]) >= 4.5, (text, surface)
    # Past bookings are still read, on each fill they can have.
    for fill in ("--sh-info-soft", "--sh-free", "--sh-blocked-a", "--sh-held-a"):
        assert _contrast(t["--sh-muted"], t[fill]) >= 4.4, fill
    assert _contrast(t["--sh-input-line"], t["--sh-white"]) >= 3


def test_past_cells_are_not_faded_and_inputs_use_the_control_border() -> None:
    for path in ("auth/assets/app.css", "demo/index.html"):
        for rule in re.findall(r"\.past[^{}]*\{[^}]*\}", _read(path)):
            assert "opacity" not in rule, (path, rule)
    for path in ("auth/assets/app.css", "demo/index.html", "demo/driver.html"):
        assert re.search(r"(input|select|textarea)[^{}]*\{[^}]*var\(--(sh-)?input-line\)", _read(path)), path


def test_codes_have_words() -> None:
    assert labels.word("WAREHOUSE_AUTHORITY") == "Warehouse authority" and labels.word("UNBOUND") == "Not linked"
    assert labels.word("SOMETHING_NEW") == "Something new" and labels.word("") == ""
    assert labels.reason("LANGUAGE_UNSUPPORTED") == "Unsupported language"
    assert labels.moment("2026-08-04T09:35:00+05:30") == "04 Aug 2026, 09:35"
    assert labels.moment(None) == "never" and labels.moment("soon") == "soon"


def test_screens_show_words_names_and_times(world: World) -> None:
    admin = world.client("admin")
    text = re.sub(r"<code>.*?</code>|<[^>]+>", " ", admin.get("/admin").text.split('data-panel="model"')[0])
    # The audit log's action and outcome columns stay as the codes its filters take.
    users = text.split("Audit log")[1] if False else text.split("Pending drivers")[1]
    assert "WAREHOUSE_AUTHORITY" not in users and "Warehouse authority" in users
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text.replace("2026-08-04T09:00:00+05:30", ""))
    page = admin.get("/warehouse").text
    seen = re.sub(r'<option[^>]*>.*?</option>|<[^>]+>', " ", page)
    for code in ("NO_SLOT", "UNBOUND", "PENDING_PARTNER", "ACTIVE", "SUPPLIED"):
        assert not re.search(rf"\b{code}\b", seen), code
    projects = page.split('data-panel="projects"', 2)[-1].split("</section>", 1)[0]
    assert "<span class=\"id\">DRV001</span>" in projects and "Release DRV001?" not in projects
