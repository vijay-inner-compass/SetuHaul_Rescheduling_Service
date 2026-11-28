"""An approved move that never becomes a booking goes back; the driver hears about every step, wherever they are."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from setuhaul import facility_change as fc
from setuhaul.auth import users
from setuhaul.db import open_connection
from setuhaul.demo_ui import build_demo_app
from setuhaul.gps import store_ping
from setuhaul.jobs.expire_holds import MaintenanceRunner
from setuhaul.jobs.expire_partner_requests import run_expiry_once
from setuhaul.position import handle_new_position
from setuhaul.schemas import GpsPing
from setuhaul.stub_model import StubGateway
from tests.auth_helpers import sign_in
from tests.test_auto_hold import Chat
from tests.test_demo_driver import _client
from tests.test_facility_follow_up import GGN, JAI, PARTNER, _approve, _dest, _last_message


@pytest.fixture()
def chat(tmp_path: Path) -> Chat:
    return Chat(tmp_path)


def _sweep(chat: Chat) -> None:
    conn = open_connection(path=chat.deps.db_path)
    try:
        MaintenanceRunner(conn, chat.deps.clock).run_once()
    finally:
        conn.close()


def _request(chat: Chat) -> tuple[Any, ...]:
    return tuple(chat.rows("select status, reason from facility_change_requests")[0])


def _driver_notices(chat: Chat) -> list[str]:
    rows = chat.rows("select payload_json from notification_outbox where recipient_type='DRIVER' and recipient_ref='DRV006' order by outbox_id")
    return [str(json.loads(r[0]).get("text", "")) for r in rows]


def _admin(chat: Chat) -> tuple[Any, Any]:
    conn = open_connection(path=chat.deps.db_path)
    return conn, users.bootstrap_admin(conn, "boss@x.com", chat.deps.clock)


# -- the load never stays pointed at a site where it has no booking --------------------------------------

def test_a_lapsed_offer_sends_the_load_back_at_the_next_sweep(chat: Chat) -> None:
    old = chat.current()
    _approve(chat, "Gurugram")
    _sweep(chat)
    assert _dest(chat) == GGN  # still waiting for the driver
    chat.deps.clock.advance(timedelta(minutes=16))
    _sweep(chat)
    assert _dest(chat) == GGN and _request(chat) == ("APPROVED", "NOT_CONFIRMED")
    assert tuple(chat.rows(f"select appointment_status, is_current from appointments where slot_id='{old}'")[0]) == ("CANCELLED", 0)
    assert "assigned to SetuHaul Gurugram" in _last_message(chat)


def test_a_late_yes_sends_the_load_back_without_waiting_for_a_sweep(chat: Chat) -> None:
    old = chat.current()
    _approve(chat, "Gurugram")
    chat.deps.clock.advance(timedelta(minutes=16))
    assert not chat.say("Yes").startswith("Your appointment is confirmed")
    assert _dest(chat) == GGN
    assert tuple(chat.rows(f"select appointment_status, is_current from appointments where slot_id='{old}'")[0]) == ("CANCELLED", 0)


def test_declining_every_slot_at_the_new_site_sends_the_load_back(chat: Chat) -> None:
    old = chat.current()
    _approve(chat, "Gurugram")
    for _ in range(40):
        if not chat.held():
            break
        chat.say("No")
    assert chat.held() == [] and _dest(chat) == GGN
    assert tuple(chat.rows(f"select appointment_status, is_current from appointments where slot_id='{old}'")[0]) == ("CANCELLED", 0)


def test_a_confirmed_move_stays_moved(chat: Chat) -> None:
    _approve(chat, "Gurugram")
    assert chat.say("Yes, confirm").startswith("Your appointment is confirmed")
    chat.deps.clock.advance(timedelta(hours=30))
    _sweep(chat)
    assert _dest(chat) == GGN and _request(chat)[0] == "APPROVED"


def test_an_unanswered_partner_request_sends_the_load_back_when_it_expires(chat: Chat) -> None:
    _approve(chat, "the partner")
    _sweep(chat)
    assert _dest(chat) == PARTNER
    chat.deps.clock.advance(timedelta(days=2))
    conn = open_connection(path=chat.deps.db_path)
    try:
        run_expiry_once(conn, chat.deps.clock)
    finally:
        conn.close()
    assert _dest(chat) == PARTNER and _request(chat) == ("APPROVED", "NOT_CONFIRMED")


def test_a_hold_for_the_old_site_cannot_be_confirmed_once_the_destination_changed(chat: Chat) -> None:
    old = chat.current()
    chat.say("I will be about two hours late")
    chat.say("please change facility to Gurugram")
    conn, admin = _admin(chat)
    rid = conn.execute("select request_id from facility_change_requests").fetchone()[0]
    conn.execute("delete from gps_pings")  # without a position nothing else would notice the move
    conn.commit()
    fc.decide(conn, chat.deps.clock, actor=admin, request_id=rid, approve=True)  # the follow-up has not run yet
    conn.close()
    assert not chat.say("Yes, confirm").startswith("Your appointment is confirmed")
    assert chat.held() == []
    assert tuple(chat.rows(f"select appointment_status, is_current from appointments where slot_id='{old}'")[0]) == ("CANCELLED", 0)


def test_a_follow_up_that_crashes_puts_the_load_back_and_opens_a_case(chat: Chat, monkeypatch: pytest.MonkeyPatch) -> None:
    chat.say("please change facility to Gurugram")
    conn, admin = _admin(chat)
    web = _client(chat.app)
    sign_in(web, conn, admin, chat.deps.clock)
    conn.close()

    def boom(*_: Any, **__: Any) -> str:
        raise RuntimeError("redis is down")

    monkeypatch.setattr(fc, "after_approval", boom)
    rid = web.get("/v1/warehouse/facility-changes").json()["requests"][0]["request_id"]
    r = web.post(f"/v1/warehouse/facility-changes/{rid}/approve", json={})
    assert r.status_code == 200 and r.json()["outcome"] == "NO_SLOT"
    assert _dest(chat) == GGN and _request(chat) == ("APPROVED", "FOLLOW_UP_FAILED")
    assert chat.rows("select count(*) from escalation_cases where shipment_id='SHP1006'")[0][0] == 1
    assert "freed" in _last_message(chat)


# -- Telegram drivers only receive what is queued --------------------------------------------------------

@pytest.mark.parametrize("to_words", ["Gurugram", "the partner"])
def test_the_answer_to_a_change_request_is_queued_for_the_drivers_phone(chat: Chat, to_words: str) -> None:
    _approve(chat, to_words)
    assert _last_message(chat) in _driver_notices(chat)


def test_a_rejection_is_queued_for_the_drivers_phone(chat: Chat) -> None:
    chat.say("please change facility to Gurugram")
    conn, admin = _admin(chat)
    rid = conn.execute("select request_id from facility_change_requests").fetchone()[0]
    fc.decide(conn, chat.deps.clock, actor=admin, request_id=rid, approve=False)
    fc.tell_rejected(conn, chat.deps.redis, chat.deps.clock, rid)
    conn.close()
    assert "not approved" in _driver_notices(chat)[-1]


def test_a_closer_truck_offer_is_queued_for_the_drivers_phone(tmp_path: Path) -> None:
    _app, deps = build_demo_app(tmp_path, fresh=True, live_factory=StubGateway)
    from setuhaul.memory import ConversationMemoryStore

    conn = open_connection(path=deps.db_path)
    try:
        fac = conn.execute("select f.lat, f.lon from shipments s join facilities f on f.facility_id=s.destination_facility_id "
                           "where s.shipment_id='SHP1006'").fetchone()
        slot = conn.execute("select slot_id from appointment_slots s join docks d using(dock_id) where d.facility_id=? "
                            "order by slot_start_ts desc limit 1", (JAI,)).fetchone()[0]
        now = deps.clock.now().isoformat()
        conn.execute("insert into appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source, is_current, "
                     "booked_at, updated_at) values ('APT-LATE','SHP1006',?,'CONFIRMED','PLANNER',1,?,?)", (slot, now, now))
        store_ping(conn, "SHP1006", GpsPing(lat=fac["lat"] + 20 / 111.2, lon=fac["lon"], recorded_at=deps.clock.now()), deps.clock, "TEST")
        conn.commit()
        memory = ConversationMemoryStore(conn, deps.redis, deps.clock)
        handle_new_position(conn, "SHP1006", deps.clock, memory)
        rows = conn.execute("select payload_json from notification_outbox where recipient_type='DRIVER'").fetchall()
    finally:
        conn.close()
    assert any("earlier slot" in json.loads(r[0]).get("text", "") for r in rows)


# -- in the driver's language ----------------------------------------------------------------------------

@pytest.mark.parametrize("lang,expected", [("hi", "मंज़ूर"), ("hinglish", "manzoor")])
def test_the_approval_message_is_in_the_drivers_language(chat: Chat, lang: str, expected: str) -> None:
    chat.say("please change facility to Gurugram")
    conn, admin = _admin(chat)
    conn.execute("update conversation_state set state_json=json_set(state_json,'$.language',?) where driver_id='DRV006'", (lang,))
    conn.commit()
    chat.deps.redis.flushall()
    rid = conn.execute("select request_id from facility_change_requests").fetchone()[0]
    fc.decide(conn, chat.deps.clock, actor=admin, request_id=rid, approve=True)
    fc.after_approval(conn, chat.deps.redis, chat.deps.clock, rid)
    conn.close()
    assert expected in _last_message(chat)
