"""A refused request says which field or date rule is wrong, without repeating what was typed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from setuhaul.clock import FakeClock
from setuhaul.db import parse_operational_timestamp as ts
from setuhaul.runtime_settings import SettingsSource
from tests.auth_helpers import World, build_world
from tests.runtime_helpers import BASE
from tests.shipment_helpers import D1, NOW

AVAILABILITY = {"facility_id": "FAC-JAI-01", "first_date": "2026-11-28", "last_date": "2026-11-28", "dock_ids": [D1],
                "from_time": "09:00", "to_time": "13:00", "crews": 4}


@pytest.fixture
def world(tmp_path: Path) -> World:
    return build_world(tmp_path, FakeClock(ts(NOW)))


def _post(world: World, who: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    response = world.client(who).post(path, json=body)
    assert response.status_code == 422, response.text
    return dict(response.json())


@pytest.mark.parametrize(("over", "sentence"), [
    ({"first_date": "2026-11-26"}, "The first date (26 Nov 2026) is before today (27 Nov 2026)."),
    ({"last_date": "2026-11-27"}, "The last date (27 Nov 2026) is before the first date (28 Nov 2026)."),
    ({"last_date": "2026-12-12"},
     "The dates cover 15 days (28 Nov 2026 to 12 Dec 2026). Open at most 14 days at a time."),
])
def test_each_date_mistake_has_its_own_sentence(world: World, over: dict[str, str], sentence: str) -> None:
    answer = _post(world, "wh_a", "/v1/warehouse/availability", AVAILABILITY | over)
    assert answer["reason_code"] == "INVALID_DATES"
    assert answer["message"] == f"{sentence} Nothing was changed."


def test_every_bad_field_is_named_with_its_limit(world: World) -> None:
    body = {k: v for k, v in AVAILABILITY.items() if k != "crews"} | {"slot_minutes": 5}
    answer = _post(world, "wh_a", "/v1/warehouse/availability", body)
    assert answer["reason_code"] == "INVALID_REQUEST"
    assert answer["problems"] == [{"field": "slot_minutes", "problem": "must be 15 or more"},
                                  {"field": "crews", "problem": "is missing"}]
    assert answer["message"] == "Slot minutes: must be 15 or more. Crews: is missing. Nothing was changed."


def test_neither_values_nor_unknown_names_are_repeated(world: World) -> None:
    body = AVAILABILITY | {"first_date": "hunter2-secret", "sneaky_name": "x"}
    response = world.client("wh_a").post("/v1/warehouse/availability", json=body)
    assert "hunter2" not in response.text and "sneaky_name" not in response.text
    assert {"field": "first_date", "problem": "is not a valid date"} in response.json()["problems"]


def test_a_body_that_is_not_json_still_gets_a_sentence(world: World) -> None:
    response = world.client("wh_a").post("/v1/warehouse/availability", content="{nope",
                                         headers={"content-type": "application/json"})
    assert response.json()["message"] == "The request could not be read. Nothing was changed."


def test_a_settings_check_names_the_field_only(world: World) -> None:
    world.deps.settings = SettingsSource(BASE)
    answer = _post(world, "admin", "/v1/admin/settings/telegram", {"bot_token": "123:abc", "webhook_secret": "short"})
    assert answer["reason_code"] == "INVALID_REQUEST"
    assert [p["field"] for p in answer["problems"]] == ["webhook_secret"]
    assert answer["problems"][0]["problem"] == "is not in the expected format" and "123:abc" not in str(answer)


def test_a_hand_written_project_check_names_the_field(world: World) -> None:
    body = {"customer_name": "Acme", "contract_ref": "C-9", "facility_id": "FAC-JAI-01",
            "starts_on": "2026-12-01", "ends_on": "2026-11-30"}
    answer = _post(world, "admin", "/v1/projects", body)
    assert answer["problems"] == [{"field": "ends_on", "problem": "is before the start date"}]
