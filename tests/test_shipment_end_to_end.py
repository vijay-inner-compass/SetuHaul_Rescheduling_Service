"""A new driver and a new shipment on a date after the sample data: confirm the order, report a delay, book the slot."""

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
from setuhaul.identity import provision_identities
from setuhaul.model_gateway import ModelPolicy
from setuhaul.stub_model import StubGateway
from tests.auth_helpers import build_world
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
    assert "ORD-261127-001" in send("ORD-261127-001")

    held = send("I am running late by 60 min")
    assert query(world, "select count(*) from appointments where shipment_id=? and appointment_status='HELD'", sid)[0][0] == 1, held

    booked = send("Yes")
    current = query(world, "select a.appointment_status, a.booking_source, s.slot_start_ts from appointments a "
                           "join appointment_slots s using(slot_id) where a.shipment_id=? and a.is_current=1", sid)
    assert len(current) == 1, booked
    assert (current[0]["appointment_status"], current[0]["booking_source"]) == ("CONFIRMED", "DRIVER_CHAT")
    assert ts(current[0]["slot_start_ts"]) >= ts("2026-11-28T13:00:00+05:30")
