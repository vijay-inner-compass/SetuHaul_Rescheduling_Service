"""A pass is issued with the shipment and sent by the bot; the warehouse can send a new one or revoke it."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

from setuhaul.api import create_app
from setuhaul.auth import telegram
from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from setuhaul.db import parse_operational_timestamp as ts
from setuhaul.dock_pass import find, hash_code, normalise
from tests.auth_helpers import World, build_world, sign_in
from tests.shipment_helpers import NOW, count, open_av, query, slot_at
from tests.test_passes import everything
from tests.test_shipment_routes import _form
from tests.test_telegram_channel import SECRET, contact_update, post

# DRV001's number as the sample data holds it.
PHONE_SQL = "select phone from drivers where driver_id='DRV001'"


class Down(telegram.FakeTelegramClient):
    def send(self, chat_id: int, text: str, request_contact_button: bool = False) -> None:
        raise telegram.TelegramSendError("sendMessage failed: ConnectError")


class Rig:
    def __init__(self, tmp_path: Path, bot: telegram.FakeTelegramClient) -> None:
        self.world: World = build_world(tmp_path, FakeClock(ts(NOW)))
        self.bot = bot
        deps = replace(self.world.deps, redis=fakeredis.FakeRedis(decode_responses=True), telegram_secret=SECRET.encode(), telegram=bot)
        self.app = create_app(deps)
        open_av(self.world)

    def client(self, who: str | None = "wh_a") -> TestClient:
        client = TestClient(self.app, base_url="http://localhost", follow_redirects=False)
        if who:
            conn = open_connection(path=self.world.path)
            sign_in(client, conn, self.world.users[who], self.world.clock)
            conn.close()
        return client

    def link(self) -> None:
        query(self.world, "insert into telegram_identities(telegram_user_id, chat_id, driver_id, bound_at) values ('777','777','DRV001',?)", NOW)

    def make(self) -> str:
        response = self.client().post("/v1/warehouse/shipments", json=_form(slot_at(self.world, 12)))
        assert response.status_code == 201, response.text
        assert not find(response.text)
        return str(response.json()["shipment_id"])

    def codes(self) -> list[str]:
        return [c for _, text, _ in self.bot.sent for c in find(text)]

    def live_hash(self) -> str | None:
        rows = query(self.world, "select token_hash from dock_access_authorizations where status='ISSUED'")
        return str(rows[0][0]) if rows else None

    def row(self, sid: str) -> dict[str, object]:
        return next(s for s in self.client().get("/v1/warehouse/shipments").json()["shipments"] if s["shipment_id"] == sid)


@pytest.fixture()
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path, telegram.FakeTelegramClient())


def test_creating_sends_the_code_and_keeps_only_its_hash(rig: Rig) -> None:
    rig.link()
    sid = rig.make()
    assert len(rig.bot.sent) == 1 and rig.bot.sent[0][0] == 777 and "ORD-261127-001" in rig.bot.sent[0][1]
    [code] = rig.codes()
    assert hash_code(code) == rig.live_hash()
    assert normalise(code)[3:] not in everything(rig.world)
    assert rig.row(sid)["delivered"] is True and rig.row(sid)["pass_state"] == "ISSUED"
    assert count(rig.world, "audit_log", "action='PASS_DELIVERED'") == 1


def test_an_unlinked_driver_gets_it_on_linking(rig: Rig) -> None:
    sid = rig.make()
    assert rig.bot.sent == [] and rig.row(sid)["delivered"] is False
    assert "Pass not delivered" in rig.client().get("/warehouse").text
    first = rig.live_hash()
    phone = query(rig.world, PHONE_SQL)[0][0]
    assert post(rig.client(None), contact_update(phone=phone, user=777, contact_user=777)).status_code == 200
    [code] = rig.codes()
    assert hash_code(code) == rig.live_hash() != first and rig.row(sid)["delivered"] is True


def test_telegram_being_down_does_not_stop_creation(tmp_path: Path) -> None:
    rig = Rig(tmp_path, Down())
    rig.link()
    sid = rig.make()
    assert rig.row(sid)["delivered"] is False
    assert count(rig.world, "audit_log", "action='PASS_DELIVERY_FAILED'") == 1


def test_send_new_pass_replaces_the_code_and_asks_for_it_again(rig: Rig) -> None:
    rig.link()
    sid = rig.make()
    query(rig.world, "update shipment_assignments set confirmed_at=?, failed_attempts=2", NOW)
    first = rig.live_hash()
    assert rig.client().post(f"/v1/warehouse/shipments/{sid}/pass", json={}).status_code == 200
    assert rig.live_hash() != first and len(rig.codes()) == 2
    row = query(rig.world, "select confirmed_at, failed_attempts from shipment_assignments")[0]
    assert tuple(row) == (None, 0)
    assert rig.client("wh_b").post(f"/v1/warehouse/shipments/{sid}/pass", json={}).status_code == 404
    query(rig.world, "update truck_visits set status='CHECKED_IN'")
    refused = rig.client().post(f"/v1/warehouse/shipments/{sid}/pass", json={})
    assert refused.status_code == 409 and refused.json()["reason_code"] == "VISIT_IN_PROGRESS" and refused.json()["message"]


def test_revoking_needs_a_reason(rig: Rig) -> None:
    sid = rig.make()
    client = rig.client()
    assert client.post(f"/v1/warehouse/shipments/{sid}/pass/revoke", json={}).status_code == 422
    assert client.post(f"/v1/warehouse/shipments/{sid}/pass/revoke", json={"reason": " "}).json()["reason_code"] == "REASON_REQUIRED"
    assert client.post(f"/v1/warehouse/shipments/{sid}/pass/revoke", json={"reason": "phone lost"}).status_code == 200
    assert rig.row(sid)["pass_state"] == "REVOKED"
    page = client.get("/warehouse").text
    assert "Revoked" in page and not re.search(r"DAP-[A-Z2-7]{5}-", page)
