"""The HMAC phone channel applies the same driver eligibility as allocation and the bot (approval and licence expiry)."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import json
from datetime import timedelta

from setuhaul.auth import onboarding, users
from setuhaul.db import open_connection
from setuhaul.identity import provision_identities
from tests.test_api import PHONE, env, sign  # noqa: F401 - env is a pytest fixture

NEW_PHONE = "+91 90000 10099"


def _onboard(path, clock, days: int = 5) -> str:  # type: ignore[no-untyped-def]
    # An onboarded, approved driver whose licence runs out in `days`, mapped into the phone channel.
    conn = open_connection(path=path)
    try:
        fac = conn.execute("select facility_id from facilities order by facility_id").fetchone()[0]
        admin = users.bootstrap_admin(conn, "boss@x.com", clock)
        wh = users.invite(conn, actor=admin, email="wh@x.com", role="WAREHOUSE_AUTHORITY", display_name="W",
                          facilities=(fac,), clock=clock)
        veh = conn.execute("select vehicle_id, carrier_id from vehicles where active_flag=1 order by vehicle_id").fetchone()
        did = onboarding.add_driver(conn, actor=wh, name="Ravi", phone=NEW_PHONE, licence_number="LIC-HMAC-1",
                                    licence_expiry=clock.now().date() + timedelta(days=days), carrier_id=veh["carrier_id"],
                                    vehicle_id=veh["vehicle_id"], clock=clock)
        onboarding.approve(conn, actor=admin, driver_id=did, clock=clock)
        provision_identities(conn)
        return did
    finally:
        conn.close()


def _status(client, clock, phone: str):  # type: ignore[no-untyped-def]
    t = int(clock.now().timestamp())
    return client.get("/v1/driver/status", params={"phone": phone, "ts": t}, headers={"x-signature": sign(f"{phone}|{t}".encode())})


def _post(client, path: str, body: dict) -> object:  # type: ignore[no-untyped-def, type-arg]
    raw = json.dumps(body).encode()
    return client.post(path, content=raw, headers={"x-signature": sign(raw)})


def test_an_approved_driver_with_a_valid_licence_is_served(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    _, client, _, clock, path = env
    _onboard(path, clock)
    assert _status(client, clock, NEW_PHONE).status_code == 200


def test_an_expired_licence_is_refused_on_every_driver_route(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    _, client, calls, clock, path = env
    _onboard(path, clock, days=5)
    clock.advance(timedelta(days=6))
    # Refused exactly like an inactive driver: 403 DRIVER_NOT_ALLOWED, and the model is never called.
    status = _status(client, clock, NEW_PHONE)
    message = _post(client, "/v1/driver/messages", {"from": NEW_PHONE, "message_id": "M-EXP", "text": "any slot?"})
    location = _post(client, "/v1/driver/location", {"from": NEW_PHONE, "lat": 27.0, "lon": 75.8,
                                                      "recorded_at": clock.now().isoformat()})
    for r in (status, message, location):
        assert r.status_code == 403 and r.json()["reason_code"] == "DRIVER_NOT_ALLOWED"  # type: ignore[attr-defined]
    assert calls.n == 0
    # A supplied driver (no onboarding profile) stays eligible.
    assert _status(client, clock, PHONE).status_code == 200


def test_a_suspended_onboarded_driver_is_refused(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    _, client, _, clock, path = env
    did = _onboard(path, clock, days=300)
    conn = open_connection(path=path)
    try:
        # Rejected profile while the drivers row still says ACTIVE (eligibility must not rest on driver_status alone).
        conn.execute("update driver_profiles set approval_status='REJECTED' where driver_id=?", (did,))
        conn.commit()
    finally:
        conn.close()
    r = _status(client, clock, NEW_PHONE)
    assert r.status_code == 403 and r.json()["reason_code"] == "DRIVER_NOT_ALLOWED"
