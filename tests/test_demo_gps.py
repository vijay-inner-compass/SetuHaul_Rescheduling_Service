"""The demo console's GPS backend: seeded pings, the send and readout routes, the activity feed, and the ETA effects."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from setuhaul.db import open_connection
from setuhaul.demo_ui import build_demo_app
from setuhaul.gps import active_shipment_for_driver
from setuhaul.schemas import EtaEvidence
from setuhaul.stub_model import StubGateway  # a scripted stand-in model, for tests only
from setuhaul.tools import ReportArgs, _eta_for


# The demo app with a client that counts as this machine.
@pytest.fixture()
def demo(tmp_path: Path):  # type: ignore[no-untyped-def]
    app, deps = build_demo_app(tmp_path / "demo", live_factory=StubGateway)
    return TestClient(app), deps


# Id of the driver with this name (same helper pattern as test_demo_ui.py).
def driver_id(client: TestClient, name: str) -> str:
    return next(d["id"] for d in client.get("/demo/api/drivers").json()["drivers"] if d["name"] == name)


# Send one chat message through the real pipeline.
def say(client: TestClient, did: str, text: str) -> dict:  # type: ignore[type-arg]
    r = client.post("/demo/api/chat", json={"driver_id": did, "text": text})
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


# A position `km` straight north of the driver's destination dock.
def north_of_dock(client: TestClient, did: str, km: float) -> dict[str, float]:
    fac = client.get(f"/demo/api/driver/{did}/gps").json()["facility"]
    return {"lat": fac["lat"] + km / 111.2, "lon": fac["lon"]}


# Send a ping from the demo console.
def send(client: TestClient, did: str, km: float, age: int = 0):  # type: ignore[no-untyped-def]
    return client.post("/demo/api/gps", json={"driver_id": did, **north_of_dock(client, did, km), "age_minutes": age})


# Run the real ETA derivation against the demo's own database and clock, as the tools do.
def eta_for(deps, did: str, report: ReportArgs):  # type: ignore[no-untyped-def]
    conn = open_connection(path=deps.demo.db_path, readonly=True)
    try:
        sid = active_shipment_for_driver(conn, did)
        return _eta_for(SimpleNamespace(conn=conn, clock=deps.demo.clock), sid, report)
    finally:
        conn.close()


# --- seeding and readout -----------------------------------------------------------------------------------
def test_after_reset_every_active_driver_has_a_fresh_seeded_ping_and_readout(demo) -> None:  # type: ignore[no-untyped-def]
    client, deps = demo
    client.post("/demo/api/reset")
    # Active drivers who have a load being driven (some listed drivers have none, so there is nothing to ping).
    drivers = [d for d in client.get("/demo/api/drivers").json()["drivers"] if d["shipments"]]
    assert len(drivers) >= 10
    # A driver with no load gets a clear 404 rather than a made-up readout.
    idle = [d for d in client.get("/demo/api/drivers").json()["drivers"] if not d["shipments"]]
    assert all(client.get(f"/demo/api/driver/{d['id']}/gps").status_code == 404 for d in idle)
    for d in drivers:
        r = client.get(f"/demo/api/driver/{d['id']}/gps")
        assert r.status_code == 200, d["name"]
        out = r.json()
        # 60 km out, fresh, with a travel time and a GPS ETA.
        assert out["ping"]["fresh"] is True and out["ping"]["age_minutes"] == 0
        assert out["distance_km"] == pytest.approx(60.0, abs=0.2)
        assert out["travel_minutes"] == 117 and out["gps_eta"]
        assert out["facility"]["name"]
    conn = open_connection(path=deps.demo.db_path, readonly=True)
    try:
        assert {r["source"] for r in conn.execute("select source from gps_pings")} == {"DEMO_FIXTURE"}
        assert conn.execute("select count(*) from gps_pings").fetchone()[0] == len(drivers)
    finally:
        conn.close()


def test_a_stale_ping_has_no_travel_estimate_but_still_shows_distance(demo) -> None:  # type: ignore[no-untyped-def]
    client, _ = demo
    did = driver_id(client, "Manoj Sharma")
    r = send(client, did, 60, age=20)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ping"]["fresh"] is False and out["ping"]["age_minutes"] == 20
    assert out["travel_minutes"] is None and out["gps_eta"] is None
    assert out["distance_km"] == pytest.approx(60.0, abs=0.2)


def test_a_fresh_sent_ping_gives_travel_time_and_eta(demo) -> None:  # type: ignore[no-untyped-def]
    client, _ = demo
    did = driver_id(client, "Manoj Sharma")
    out = send(client, did, 30, age=2).json()
    assert out["ping"]["fresh"] is True and out["ping"]["age_minutes"] == 2
    assert out["distance_km"] == pytest.approx(30.0, abs=0.2) and out["travel_minutes"] == 59
    assert len(out["gps_eta"]) == 5 and out["gps_eta"][2] == ":"
    # The readout route agrees with what the send returned.
    assert client.get(f"/demo/api/driver/{did}/gps").json() == out


def test_invalid_values_are_rejected(demo) -> None:  # type: ignore[no-untyped-def]
    client, _ = demo
    did = driver_id(client, "Manoj Sharma")
    base = {"driver_id": did, "lat": 26.0, "lon": 75.0, "age_minutes": 0}
    assert client.post("/demo/api/gps", json={**base, "lat": 91}).status_code == 422
    assert client.post("/demo/api/gps", json={**base, "lon": 181}).status_code == 422
    assert client.post("/demo/api/gps", json={**base, "age_minutes": 1441}).status_code == 422
    assert client.post("/demo/api/gps", json={**base, "age_minutes": -1}).status_code == 422
    assert client.post("/demo/api/gps", json={**base, "extra": 1}).status_code == 422


def test_outsiders_are_refused_on_both_gps_routes(tmp_path: Path) -> None:
    app, _ = build_demo_app(tmp_path / "d", live_factory=StubGateway)
    outsider = TestClient(app, client=("203.0.113.9", 50000))
    r = outsider.get("/demo/api/driver/DRV001/gps")
    assert r.status_code == 403 and r.json()["reason_code"] == "DEMO_IS_LOCAL_ONLY"
    r = outsider.post("/demo/api/gps", json={"driver_id": "DRV001", "lat": 26.0, "lon": 75.0, "age_minutes": 0})
    assert r.status_code == 403 and r.json()["reason_code"] == "DEMO_IS_LOCAL_ONLY"


def test_unknown_driver_is_404_on_both_routes(demo) -> None:  # type: ignore[no-untyped-def]
    client, _ = demo
    r = client.post("/demo/api/gps", json={"driver_id": "NOPE", "lat": 26.0, "lon": 75.0, "age_minutes": 0})
    assert r.status_code == 404 and r.json()["reason_code"] == "UNKNOWN_DRIVER"
    r = client.get("/demo/api/driver/NOPE/gps")
    assert r.status_code == 404 and r.json()["reason_code"] == "UNKNOWN_DRIVER"


def test_driver_with_no_active_shipment_is_404_and_nothing_is_stored(demo) -> None:  # type: ignore[no-untyped-def]
    client, deps = demo
    did = driver_id(client, "Manoj Sharma")
    # Finish all of this driver's loads in the demo database.
    conn = open_connection(path=deps.demo.db_path)
    try:
        conn.execute("update shipments set current_status='COMPLETED' where driver_id=?", (did,))
        conn.commit()
        before = conn.execute("select count(*) from gps_pings").fetchone()[0]
    finally:
        conn.close()
    r = client.post("/demo/api/gps", json={"driver_id": did, "lat": 26.0, "lon": 75.0, "age_minutes": 0})
    assert r.status_code == 404 and r.json()["reason_code"] == "NO_ACTIVE_SHIPMENT"
    assert client.get(f"/demo/api/driver/{did}/gps").json()["reason_code"] == "NO_ACTIVE_SHIPMENT"
    conn = open_connection(path=deps.demo.db_path, readonly=True)
    try:
        assert conn.execute("select count(*) from gps_pings").fetchone()[0] == before
    finally:
        conn.close()


def test_sending_a_ping_does_not_move_the_demo_clock(demo) -> None:  # type: ignore[no-untyped-def]
    client, _ = demo
    before = client.get("/demo/api/config").json()["clock"]["iso"]
    send(client, driver_id(client, "Manoj Sharma"), 60, age=5)
    assert client.get("/demo/api/config").json()["clock"]["iso"] == before


# --- the activity feed -------------------------------------------------------------------------------------
def test_activity_feed_explains_each_ping_with_names_and_arithmetic(demo) -> None:  # type: ignore[no-untyped-def]
    client, _ = demo
    did = driver_id(client, "Manoj Sharma")
    send(client, did, 60, age=2)
    gps = client.get("/demo/api/activity").json()["gps"]
    assert 1 <= len(gps) <= 10
    top = gps[0]
    assert set(top) == {"time", "text"} and len(top["time"]) == 8  # HH:MM:SS
    text = top["text"]
    assert text.startswith("Manoj Sharma · ORD-")
    for needle in ("ping 2 min old", "60.0 km from dock", "x1.3 road factor at 40 km/h = 117 min", "GPS ETA "):
        assert needle in text, text
    assert "DRV" not in text and "SHP" not in text  # never raw ids


def test_activity_feed_marks_a_stale_ping_as_unusable_and_lists_newest_first(demo) -> None:  # type: ignore[no-untyped-def]
    client, _ = demo
    did = driver_id(client, "Manoj Sharma")
    send(client, did, 60, age=2)
    send(client, did, 60, age=20)
    gps = client.get("/demo/api/activity").json()["gps"]
    assert "ping 20 min old" in gps[0]["text"] and "no GPS ETA" in gps[0]["text"]
    # The 2-minute ping was newer than the 20-minute one, so the demo dropped it; the next row is another driver's seeded fresh ping.
    assert "Manoj Sharma" not in gps[1]["text"] and "ping 0 min old" in gps[1]["text"] and "-> GPS ETA" in gps[1]["text"]


# --- scenarios through the real chat pipeline ----------------------------------------------------------------
# The stub model only understands "N minutes late", never a repair report, so scenarios (b) and (c) assert at the tool level
# (the same _eta_for the tools call, with the demo's real database and clock).
def test_scenario_a_driver_far_later_than_gps_says_gets_a_discrepancy(demo) -> None:  # type: ignore[no-untyped-def]
    client, deps = demo
    did = driver_id(client, "Manoj Sharma")
    # A ping about 120 km out: GPS says roughly four hours of driving.
    out = send(client, did, 120).json()
    assert out["travel_minutes"] == 234
    # The real pipeline still answers the "20 minutes late" message normally, and the reply now carries the GPS note.
    reply = say(client, did, "my truck is 20 minutes late")
    assert reply["status"] == "REPLIED"
    # Exact gap, worked by hand: planned 10:20 + 20 = 10:40; GPS ETA = 09:35:20 + 234 min = 13:29:20; 169 min 20 s -> 169.
    assert "differs from your report by about 169 minutes" in reply["reply"]
    # The ETA the tools derive for that same report is flagged with the same gap and a lowered confidence.
    eta = eta_for(deps, did, ReportArgs(kind="late_by", minutes=20))
    assert isinstance(eta, EtaEvidence) and eta.source == "DRIVER_DECLARED"
    assert eta.gps_discrepancy_minutes == 169
    assert eta.confidence == "LOW"


def test_scenario_b_repair_without_drive_time_and_fresh_ping_is_gps_assisted(demo) -> None:  # type: ignore[no-untyped-def]
    client, deps = demo
    did = driver_id(client, "Manoj Sharma")
    send(client, did, 30, age=1)
    eta = eta_for(deps, did, ReportArgs(kind="repair", minutes=45))
    # No travel-time question: a usable ETA from GPS.
    assert isinstance(eta, EtaEvidence) and eta.source == "GPS_ASSISTED"
    assert eta.confidence == "MEDIUM"


def test_scenario_c_same_repair_after_a_stale_ping_asks_the_travel_time_question(demo) -> None:  # type: ignore[no-untyped-def]
    client, deps = demo
    did = driver_id(client, "Manoj Sharma")
    send(client, did, 30, age=20)
    eta = eta_for(deps, did, ReportArgs(kind="repair", minutes=45))
    assert not isinstance(eta, EtaEvidence)
    assert eta.code == "MISSING_TRAVEL_TIME"


# --- a dock without coordinates ---------------------------------------------------------------------------------
# Migration 009 allows NULL lat/lon: such a facility simply gets no GPS ETA, and the console must not crash.
def test_readout_and_activity_survive_a_dock_without_coordinates(demo) -> None:  # type: ignore[no-untyped-def]
    client, deps = demo
    did = driver_id(client, "Manoj Sharma")
    conn = open_connection(path=deps.demo.db_path)
    try:
        sid = active_shipment_for_driver(conn, did)
        # Blank the coordinates of this load's destination dock (the seeded fixture ping for it is already stored).
        conn.execute("update facilities set lat=NULL, lon=NULL where facility_id=(select destination_facility_id from shipments where shipment_id=?)", (sid,))
        conn.commit()
    finally:
        conn.close()
    r = client.get(f"/demo/api/driver/{did}/gps")
    assert r.status_code == 200
    out = r.json()
    # The usual keys stay; the figures that need the dock are empty and one flag says why.
    assert out["has_coordinates"] is False
    assert out["distance_km"] is None and out["travel_minutes"] is None and out["gps_eta"] is None
    assert out["facility"]["lat"] is None and out["ping"] is not None
    # The activity feed explains it instead of failing.
    feed = client.get("/demo/api/activity")
    assert feed.status_code == 200
    assert any("dock has no coordinates" in g["text"] for g in feed.json()["gps"])


# A normal dock reports that it has coordinates.
def test_readout_flags_a_dock_with_coordinates(demo) -> None:  # type: ignore[no-untyped-def]
    client, _ = demo
    did = driver_id(client, "Manoj Sharma")
    assert client.get(f"/demo/api/driver/{did}/gps").json()["has_coordinates"] is True
