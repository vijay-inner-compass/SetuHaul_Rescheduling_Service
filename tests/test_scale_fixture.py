"""The generated scale fixture is large, valid, and never passes itself off as SetuHaul data."""

from __future__ import annotations

from pathlib import Path

import pytest

from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from setuhaul.feasibility import search_for_shipment
from setuhaul.integrity import check_invariants
from setuhaul.scale_fixture import make_scale_fixture
from tests.helpers import at, eta_at


@pytest.fixture(scope="module")
def fixture_path(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict]:  # type: ignore[type-arg]
    p = tmp_path_factory.mktemp("scale") / "scale.sqlite3"
    return p, make_scale_fixture(p)


def test_volume_matches_the_brief(fixture_path) -> None:  # type: ignore[no-untyped-def]
    _, s = fixture_path
    assert s["loads"] == 360 and s["shipments"] == 360 and s["slots_total"] > 800 and s["appointments"] > 150


def test_every_generated_row_is_marked_and_the_snapshot_is_untouched(fixture_path) -> None:  # type: ignore[no-untyped-def]
    p, _ = fixture_path
    c = open_connection(path=p, readonly=True)
    assert c.execute("select count(*) from shipments where shipment_id like 'GEN-%' and customer_name<>'GENERATED SCALE FIXTURE'").fetchone()[0] == 0
    assert c.execute("select count(*) from shipments where shipment_id not like 'GEN-%'").fetchone()[0] == 21
    assert c.execute("select count(*) from labour_windows where source<>'GENERATED_SCALE_FIXTURE'").fetchone()[0] == 0
    assert c.execute("select count(*) from drivers where driver_id like 'GEN-%' and driver_name not like 'Generated Driver%'").fetchone()[0] == 0
    c.close()


def test_fixture_is_consistent(fixture_path) -> None:  # type: ignore[no-untyped-def]
    p, _ = fixture_path
    c = open_connection(path=p, readonly=True)
    assert check_invariants(c) == [] and c.execute("pragma foreign_key_check").fetchall() == []
    assert c.execute("select count(*) from channel_identities").fetchone()[0] == 15 + 360
    c.close()


def test_burst_group_shares_one_eta_and_a_best_slot(fixture_path) -> None:  # type: ignore[no-untyped-def]
    p, s = fixture_path
    c = open_connection(path=p, readonly=True)
    clock = FakeClock(at("09:35"))
    tops = set()
    for i in range(1, 13):
        r = search_for_shipment(c, f"GEN-SHP{i:04d}", eta_at("10:00"), clock)
        assert r.status == "OK" and not any(d.status != "FEASIBLE" for d in r.feasible)
        tops.add(r.feasible[0].slot_id)
    assert len(tops) == 1 and len(s["burst"]) == 12
    c.close()
