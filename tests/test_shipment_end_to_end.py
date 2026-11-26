"""A new driver and shipment on a date after the sample data: send the pass, rebook, pass the gate, unload and leave."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import fakeredis
from fastapi.testclient import TestClient

from setuhaul.api import create_app
from setuhaul.auth import onboarding, telegram
from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from setuhaul.db import parse_operational_timestamp as ts
from setuhaul.dock_pass import find, normalise
from setuhaul.identity import provision_identities
from setuhaul.model_gateway import ModelPolicy
from setuhaul.stub_model import StubGateway
from tests.auth_helpers import add_user, build_world, sign_in
from tests.shipment_helpers import NOW, create, open_av, query, slot_at
from tests.test_telegram_channel import SECRET, contact_update, post, text_update

PHONE = "+919811155555"


def test_from_availability_to_a_confirmed_booking(tmp_path: Path) -> None:
    world = build_world(tmp_path, FakeClock(ts(NOW)))
    conn = open_connection(path=world.path)
    driver = onboarding.add_driver(conn, actor=world.users["wh_a"], name="New Driver", phone=PHONE, licence_number="LIC-NEW-1",
                                   licence_expiry=world.clock.now().date() + timedelta(days=300), carrier_id="CAR001",
                                   vehicle_id="VEH001", clock=world.clock)
    onboarding.approve(conn, actor=world.users["admin"], driver_id=driver, clock=world.clock)
    provision_identities(conn)
    conn.close()
    bot = telegram.FakeTelegramClient()
    deps = replace(world.deps, redis=fakeredis.FakeRedis(decode_responses=True), policy=ModelPolicy(provider="stub", model_id="stub"),
                   gateway_factory=StubGateway, telegram_secret=SECRET.encode(), telegram=bot)
    client = TestClient(create_app(deps))

    def send(text: str) -> str:
        assert post(client, text_update(text, user=555)).status_code == 200
        return bot.sent[-1][1]

    open_av(world, to_time="17:00")
    sid = create(world, slot_at(world, 12), driver_id=driver)
    assert post(client, contact_update(phone=PHONE, user=555, contact_user=555)).status_code == 200
    # Linking sends the pass; the driver sends it straight back.
    [code] = find(bot.sent[-1][1])
    assert "ORD-261127-001" in send(code)

    held = send("I am running late by 60 min")
    assert query(world, "select count(*) from appointments where shipment_id=? and appointment_status='HELD'", sid)[0][0] == 1, held

    booked = send("Yes")
    current = query(world, "select a.appointment_status, a.booking_source, s.slot_start_ts from appointments a "
                           "join appointment_slots s using(slot_id) where a.shipment_id=? and a.is_current=1", sid)
    assert len(current) == 1, booked
    assert (current[0]["appointment_status"], current[0]["booking_source"]) == ("CONFIRMED", "DRIVER_CHAT")
    assert ts(current[0]["slot_start_ts"]) >= ts("2026-11-28T13:00:00+05:30")

    # At the new time the same pass admits the truck.
    conn = open_connection(path=world.path)
    world.users["guard"] = add_user(conn, world.clock, "guard@x.com", "SECURITY", ("FAC-JAI-01",))  # type: ignore[arg-type]
    conn.close()
    world.clock.set(ts(current[0]["slot_start_ts"]) - timedelta(minutes=10))  # type: ignore[attr-defined]
    guard = TestClient(client.app, base_url="http://localhost")
    conn = open_connection(path=world.path)
    sign_in(guard, conn, world.users["guard"], world.clock)
    sign_in(client, conn, world.users["wh_a"], world.clock)
    registration = conn.execute("select registration_number from vehicles where vehicle_id='VEH001'").fetchone()[0]
    conn.close()
    arrival = {"code": code, "vehicle_registration": registration, "licence_number": "LIC-NEW-1", "request_key": "e2e"}
    checked = guard.post("/v1/gate/validate", json=arrival)
    assert checked.status_code == 200, checked.text
    visit = checked.json()["truck_visit_id"]
    assert guard.post("/v1/gate/check-in", json=arrival).status_code == 200
    done = client.post(f"/v1/warehouse/visits/{visit}/unloading-complete", json={})
    assert done.status_code == 200, done.text
    assert guard.post("/v1/gate/check-out", json={"truck_visit_id": visit, "request_key": "out"}).status_code == 200
    assert guard.post("/v1/gate/validate", json=arrival).json()["reason_code"] == "PASS_NOT_VALID"

    conn = open_connection(path=world.path)
    names = [r[0] for r in conn.execute("select name from sqlite_master where type='table'")]
    everything = " ".join(str(tuple(row)) for n in names for row in conn.execute(f'select * from "{n}"'))
    conn.close()
    assert normalise(code)[3:] not in normalise(everything)
