"""Waiting work is visible, choices are remembered, forms cannot pick the impossible (UX-08, 12, 13, 15, 20, 32)."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import re
from pathlib import Path

import pytest

from setuhaul.auth import audit
from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from tests.auth_helpers import GURUGRAM, JAIPUR, World, build_world

ASSETS = Path(__file__).resolve().parents[1] / "src" / "setuhaul" / "auth" / "assets"


@pytest.fixture()
def world(tmp_path: Path, clock: FakeClock) -> World:
    return build_world(tmp_path, clock)


def _get(world: World, who: str, path: str) -> str:
    r = world.client(who).get(path)
    assert r.status_code == 200
    return str(r.text)


def _attention(page: str) -> dict[str, int]:
    block = re.search(r'<ul class="attention".*?</ul>', page, re.DOTALL)
    assert block
    return {label: int(n) for n, label in re.findall(r"<b>(\d+)</b><span>([^<]+)</span>", block.group(0))}


def _count(page: str, key: str) -> str:
    found = re.search(rf'<span class="count {key}-count">(\d*)</span>', page)
    assert found, key
    return found.group(1)


def test_admin_screen_shows_every_waiting_queue(world: World) -> None:
    page = _get(world, "admin", "/admin")
    waiting = _attention(page)
    # Three open cases in the world; the one that names no load is the admin's alone to pick up.
    assert waiting["Open cases"] == 3 and waiting["Cases with no load"] == 1
    assert "Drivers awaiting approval" in waiting and "Facility requests" in waiting
    assert _count(page, "pending") == ""


def test_messages_tab_carries_its_count(world: World) -> None:
    assert _count(_get(world, "wh_a", "/warehouse"), "messages") == "1"
    assert ".wh .messages-count" in (ASSETS / "warehouse.js").read_text(encoding="utf-8")


def test_partner_count_covers_every_facility_in_scope(world: World) -> None:
    # The admin sees the Gurugram board first, but both facilities' pending requests are waiting.
    page = _get(world, "admin", f"/warehouse?facility={GURUGRAM}")
    assert _attention(page)["Waiting for partner"] == 2


def test_the_chosen_facility_is_remembered_and_scope_still_applies(world: World) -> None:
    client = world.client("admin")
    assert client.get("/warehouse", params={"facility": JAIPUR}).status_code == 200
    page = client.get("/warehouse").text
    assert f'<option value="{JAIPUR}" selected>' in page
    # The new-project form starts on the same facility.
    form = page.split('data-api="/v1/projects"', 1)[1].split("</form>", 1)[0]
    assert f'<option value="{JAIPUR}" selected>' in form
    # A remembered facility outside the user's scope is ignored, not refused.
    other = world.client("wh_a")
    other.cookies.set("sh_facility", GURUGRAM)
    assert f'<option value="{JAIPUR}" selected>' in other.get("/warehouse").text
    # The live refresh does not move the memory.
    client.get("/warehouse", params={"facility": GURUGRAM}, headers={"X-Live": "1"})
    assert f'<option value="{JAIPUR}" selected>' in client.get("/warehouse").text


def test_invite_offers_checkboxes_and_the_driver_form_filters_vehicles(world: World) -> None:
    admin = _get(world, "admin", "/admin")
    invite = admin.split('data-api="/v1/admin/users"', 1)[1].split("</form>", 1)[0]
    assert "multiple" not in invite and f'<input type="checkbox" name="facilities" value="{JAIPUR}">' in invite
    page = _get(world, "wh_a", "/warehouse")
    assert 'data-api="/v1/drivers" data-limit' in page
    assert "carrier_id" in (ASSETS / "warehouse.js").read_text(encoding="utf-8")
    assert 'field.type === "checkbox"' in (ASSETS / "common.js").read_text(encoding="utf-8")


def _user(world: World, who: str) -> str:
    return world.users[who].user_id


def test_admin_can_undo_a_disable(world: World) -> None:
    admin = world.client("admin")
    base = f"/v1/admin/users/{_user(world, 'wh_a')}"
    assert admin.post(f"{base}/disable", json={}).status_code == 200
    assert f'data-api="{base}/enable"' in admin.get("/admin").text
    assert admin.post(f"{base}/enable", json={}).status_code == 200
    assert world.client("wh_a").get("/warehouse").status_code == 200
    conn = open_connection(path=world.path, readonly=True)
    assert [r["outcome"] for r in audit.query(conn, action="ENABLE")] == ["OK"]
    conn.close()


def test_admin_can_change_a_users_facilities(world: World) -> None:
    admin = world.client("admin")
    url = f"/v1/admin/users/{_user(world, 'wh_a')}/facilities"
    assert f'data-api="{url}"' in admin.get("/admin").text
    assert admin.post(url, json={"facilities": [GURUGRAM]}).status_code == 200
    page = world.client("wh_a").get("/warehouse").text
    assert f'value="{GURUGRAM}"' in page and world.client("wh_a").get("/warehouse", params={"facility": JAIPUR}).status_code == 404
    for body, code in (({"facilities": []}, "FACILITY_REQUIRED"), ({"facilities": ["FAC-NO"]}, "UNKNOWN_FACILITY")):
        assert admin.post(url, json=body).json()["reason_code"] == code
    own = f"/v1/admin/users/{_user(world, 'admin')}/facilities"
    assert admin.post(own, json={"facilities": [JAIPUR]}).json()["reason_code"] == "NOT_FACILITY_BOUND"


def test_only_an_admin_may_enable_or_change_facilities(world: World) -> None:
    for step, body in (("enable", {}), ("facilities", {"facilities": [GURUGRAM]})):
        url = f"/v1/admin/users/{_user(world, 'wh_a')}/{step}"
        assert world.client("wh_a").post(url, json=body).status_code == 403
        assert world.client().post(url, json=body).status_code == 401


def test_typing_in_one_form_is_not_lost_to_another_without_asking() -> None:
    script = (ASSETS / "common.js").read_text(encoding="utf-8")
    assert 'setAttribute("data-dirty"' in script and "will be lost" in script
    assert "window.SH.keep(" in (ASSETS / "settings.js").read_text(encoding="utf-8")
