"""The demonstration console's truck-position (GPS) panel: page-content checks on the served HTML."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from setuhaul.demo_ui import build_demo_app


# The served demo page as text.
@pytest.fixture()
def html(tmp_path: Path) -> str:
    app, _ = build_demo_app(tmp_path / "demo")
    return TestClient(app).get("/demo").text


# The panel's inputs, slider and button exist with the agreed ids and label.
def test_page_has_the_truck_position_controls(html: str) -> None:
    for needle in ('id="gpsLat"', 'id="gpsLon"', 'id="gpsAge"', 'id="gpsDist"', "Send ping", "Truck position"):
        assert needle in html, needle
    # The slider covers 0-300 km and the age input 0-1440 minutes, as the endpoint validates.
    assert 'id="gpsDist"' in html and 'max="300"' in html and 'max="1440"' in html


# The panel talks to the two Task 7 routes and sets the position due north of the dock.
def test_page_uses_the_gps_routes_and_due_north_formula(html: str) -> None:
    assert "/gps`" in html or "/gps\"" in html
    assert "/demo/api/gps" in html
    assert "111.2" in html


# Nothing is sent until the button is pressed: the only POST to the GPS route is inside sendPing.
def test_page_posts_only_from_send_ping(html: str) -> None:
    assert html.count('api("/demo/api/gps"') == 1
    assert "function sendPing" in html


# A driver with no open load is handled in the panel, not with an error toast.
def test_page_handles_a_driver_with_no_active_load(html: str) -> None:
    assert "NO_ACTIVE_SHIPMENT" in html and "No active load, so no GPS" in html


# Server validation errors are shown inside the panel.
def test_page_shows_gps_errors_inside_the_panel(html: str) -> None:
    assert 'id="gpsMsg"' in html


# The Activity tab renders the gps list from /demo/api/activity.
def test_page_renders_the_activity_gps_list(html: str) -> None:
    assert "a.gps" in html and "GPS pings" in html


# Everything the API returns for the panel is inserted through esc(.
def test_gps_values_from_the_api_are_escaped(html: str) -> None:
    assert "esc(g.text)" in html and "esc(g.time)" in html
    assert "esc(d.facility.name)" in html or "esc(fac.name)" in html
    assert "esc(" in html.split("function renderGps", 1)[1].split("function sendPing", 1)[0]
