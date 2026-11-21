"""Signed GPS ping endpoints: /v1/gps/pings (telematics) and /v1/driver/location (driver-shared)."""

# Lazy annotations.
from __future__ import annotations

# json encodes bodies.
import json

# sqlite3 for the Connection type.
import sqlite3

# timedelta builds the future offsets.
from datetime import timedelta

# A fresh connection proves rows are really committed.
from setuhaul.db import open_connection

# The fixture clock is 09:35; this time is a minute earlier.
from tests.helpers import at

# Fixture, signing helper and the seeded driver phone are reused from the existing API tests.
from tests.test_api import PHONE, env, sign  # noqa: F401 - env is a pytest fixture

# Where each route lives.
PINGS = "/v1/gps/pings"
LOCATION = "/v1/driver/location"


# POST a JSON body (or raw bytes) with a signature (valid by default; None means header absent).
def post_to(client, path: str, body, signature: str | None = "auto"):  # type: ignore[no-untyped-def]
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    headers = {} if signature is None else {"x-signature": sign(raw) if signature == "auto" else signature}
    return client.post(path, content=raw, headers=headers)


# A valid telematics body.
def ping_body(**changes: object) -> dict[str, object]:
    return {"shipment_id": "SHP1006", "lat": 27.0, "lon": 75.8, "recorded_at": at("09:34").isoformat(), **changes}


# A valid driver-location body.
def location_body(**changes: object) -> dict[str, object]:
    return {"from": PHONE, "lat": 27.0, "lon": 75.8, "recorded_at": at("09:34").isoformat(), **changes}


# Every stored ping, read through a brand-new connection.
def stored(path) -> list[sqlite3.Row]:  # type: ignore[no-untyped-def]
    conn = open_connection(path=path)
    try:
        return conn.execute("select * from gps_pings").fetchall()
    finally:
        conn.close()


def test_signed_ping_is_stored_and_persisted(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    _, client, _, _, path = env
    r = post_to(client, PINGS, ping_body())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "STORED" and body["request_id"]
    rows = stored(path)
    assert len(rows) == 1
    assert rows[0]["ping_id"] == body["ping_id"] and rows[0]["shipment_id"] == "SHP1006" and rows[0]["source"] == "TELEMATICS"


def test_missing_and_wrong_signature_are_401_and_store_nothing(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    _, client, _, _, path = env
    for sig in (None, "deadbeef"):
        r = post_to(client, PINGS, ping_body(), signature=sig)
        assert r.status_code == 401 and r.json()["reason_code"] == "INVALID_SIGNATURE"
    assert stored(path) == []


def test_signature_is_checked_before_parsing(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    _, client, _, _, _ = env
    for path in (PINGS, LOCATION):
        r = post_to(client, path, b"{not json", signature="deadbeef")
        assert r.status_code == 401 and r.json()["reason_code"] == "INVALID_SIGNATURE"


def test_invalid_bodies_are_422_without_echoing_values(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    _, client, _, _, path = env
    bad = [ping_body(lat=91), ping_body(lon=-181), ping_body(recorded_at="2026-11-21T09:34:00"), ping_body(extra="x")]
    for body in bad:
        r = post_to(client, PINGS, body)
        assert r.status_code == 422 and r.json()["reason_code"] == "INVALID_REQUEST"
        # Only the standard rejection keys appear: no detail, so no coordinates are echoed.
        assert set(r.json()) == {"status", "reason_code", "request_id"}
    # Malformed JSON that is correctly signed is also a 422.
    assert post_to(client, PINGS, b"{not json").status_code == 422
    assert stored(path) == []


def test_unknown_shipment_is_404(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    _, client, _, _, path = env
    r = post_to(client, PINGS, ping_body(shipment_id="SHP9999"))
    assert r.status_code == 404 and r.json()["reason_code"] == "UNKNOWN_SHIPMENT"
    assert stored(path) == []


def test_database_outage_is_503(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    _, client, _, _, path = env
    path.unlink()
    r = post_to(client, PINGS, ping_body())
    assert r.status_code == 503 and r.json()["reason_code"] == "DATABASE_UNAVAILABLE"
    assert set(r.json()) == {"status", "reason_code", "request_id"}


def test_driver_location_stores_against_the_active_shipment(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    _, client, _, _, path = env
    r = post_to(client, LOCATION, location_body())
    assert r.status_code == 200 and r.json()["status"] == "STORED"
    rows = stored(path)
    assert len(rows) == 1 and rows[0]["shipment_id"] == "SHP1006" and rows[0]["source"] == "DRIVER_SHARED"


def test_driver_location_rejects_bad_signature_and_bad_body(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    _, client, _, _, path = env
    r = post_to(client, LOCATION, location_body(), signature="deadbeef")
    assert r.status_code == 401 and r.json()["reason_code"] == "INVALID_SIGNATURE"
    for bad in (location_body(lat=91), location_body(recorded_at="2026-11-21T09:34:00"), location_body(extra="x")):
        r = post_to(client, LOCATION, bad)
        assert r.status_code == 422 and r.json()["reason_code"] == "INVALID_REQUEST"
    assert stored(path) == []


def test_driver_location_from_an_unknown_phone_is_403(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    _, client, _, _, path = env
    r = post_to(client, LOCATION, location_body(**{"from": "+91 99999 99999"}))
    assert r.status_code == 403 and r.json()["reason_code"] == "UNKNOWN_PHONE"
    assert stored(path) == []


def test_driver_location_with_a_malformed_phone_is_403_invalid_phone(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    _, client, _, _, path = env
    r = post_to(client, LOCATION, location_body(**{"from": "12345"}))
    assert r.status_code == 403 and r.json()["reason_code"] == "INVALID_PHONE"
    assert stored(path) == []


def test_ping_up_to_five_minutes_ahead_is_stored_and_beyond_is_422(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    _, client, _, clock, path = env
    now = clock.now()
    # Exactly 5 minutes and 4 minutes ahead are accepted, on both routes.
    assert post_to(client, PINGS, ping_body(recorded_at=(now + timedelta(minutes=4)).isoformat())).status_code == 200
    assert post_to(client, LOCATION, location_body(recorded_at=(now + timedelta(minutes=5)).isoformat())).status_code == 200
    assert len(stored(path)) == 2
    # One second past the limit and 6 minutes ahead are refused on both routes, and nothing more is stored.
    for route, make in ((PINGS, ping_body), (LOCATION, location_body)):
        for ahead in (timedelta(minutes=5, seconds=1), timedelta(minutes=6)):
            r = post_to(client, route, make(recorded_at=(now + ahead).isoformat()))
            assert r.status_code == 422 and r.json()["reason_code"] == "INVALID_REQUEST"
    assert len(stored(path)) == 2
    # A ping far in the past is unaffected.
    assert post_to(client, PINGS, ping_body(recorded_at=(now - timedelta(days=2)).isoformat())).status_code == 200


def test_driver_with_only_closed_shipments_gets_404_and_nothing_stored(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    _, client, _, _, path = env
    conn = open_connection(path=path)
    conn.execute("update shipments set current_status='COMPLETED' where driver_id='DRV006'")
    conn.commit()
    conn.close()
    r = post_to(client, LOCATION, location_body())
    assert r.status_code == 404 and r.json()["reason_code"] == "NO_ACTIVE_SHIPMENT"
    assert stored(path) == []
