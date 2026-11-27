"""Every outcome the driver did not cause themselves reaches their phone, in their language."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from setuhaul import driver_messages, partner_booking
from setuhaul.clock import FakeClock
from setuhaul.db import apply_migrations, make_working_db, open_connection
from setuhaul.escalation import HandoffRequest, claim_case, create_handoff, resolve_case
from setuhaul.events import queue_driver_notice
from setuhaul.gps import store_ping
from setuhaul.jobs.expire_holds import expire_due_holds
from setuhaul.language import say
from setuhaul.memory import ConversationMemoryStore, ConversationState
from setuhaul.partner_booking import expire_partner_requests, handle_reply, request_slot
from setuhaul.position import handle_new_position
from setuhaul.schemas import GpsPing
from tests.auth_helpers import add_user
from tests.conv_helpers import Chat, say_text, tc
from tests.helpers import at, seed_everything_known, seed_partner_facility

SEARCH = [tc("search_slot_options", shipment_id="SHP1006", report={"kind": "late_by", "minutes": 60}), say_text("x")]


def _notices(db: sqlite3.Connection, driver: str = "DRV006") -> list[str]:
    return [json.loads(r[0])["text"] for r in db.execute(
        "select payload_json from notification_outbox where recipient_type='DRIVER' and recipient_ref=? order by outbox_id",
        (driver,))]


def _speaks(db: sqlite3.Connection, clock: FakeClock, language: str, thread: str = "THR001") -> None:
    store = ConversationMemoryStore(db, __import__("fakeredis").FakeRedis(decode_responses=True), clock)
    store.save_structured_state("DRV006", thread, ConversationState(language=language))


# --- partner warehouse answers ---------------------------------------------------------------------------------------
@pytest.fixture()
def asked(db: sqlite3.Connection, clock: FakeClock) -> str:
    seed_partner_facility(db)
    r = request_slot(db, clock, shipment_id="SHP1006", proposed_start=at("12:00"), proposed_end=at("13:00"))
    assert r.request_ref
    return r.request_ref


def _reply(db: sqlite3.Connection, clock: FakeClock, ref: str, body: str) -> None:
    handle_reply(db, clock, message_id=f"<{uuid.uuid4().hex}@x>", from_addr="partner.desk@partner.example",
                 subject=f"[{ref}]", in_reply_to=None, body=body, authentication_results=None)


@pytest.mark.parametrize("body,key", [("APPROVE", "partner_approved"), ("DECLINE", "partner_declined")])
def test_a_partner_answer_reaches_the_phone_once_in_the_drivers_language(db: sqlite3.Connection, clock: FakeClock,
                                                                         asked: str, body: str, key: str) -> None:
    _speaks(db, clock, "hinglish")
    _reply(db, clock, asked, body)
    _reply(db, clock, asked, body)  # the partner replies twice
    assert _notices(db) == [say(key, "hinglish", facility="TEST_FIXTURE Partner Warehouse", time="12:00", date="04 August")]


def test_an_unanswered_partner_request_reaches_the_phone(db: sqlite3.Connection, clock: FakeClock, asked: str) -> None:
    clock.advance(timedelta(minutes=121))
    expire_partner_requests(db, clock)
    assert len(_notices(db)) == 1 and "did not reply in time" in _notices(db)[0]


def test_an_approval_that_cannot_be_booked_reaches_the_phone(db: sqlite3.Connection, clock: FakeClock, asked: str) -> None:
    db.execute("update docks set dock_status='OUT_OF_SERVICE' where dock_id='DOCK-PARTNER-D1'")
    db.commit()
    _reply(db, clock, asked, "APPROVE")
    assert len(_notices(db)) == 1 and "could not complete the request" in _notices(db)[0]


def test_an_approval_that_rolls_back_tells_the_driver_nothing(db: sqlite3.Connection, clock: FakeClock, asked: str,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    def crash(point: str) -> None:
        if point == "AFTER_NEW_APPOINTMENT":
            raise RuntimeError(point)

    monkeypatch.setattr(partner_booking, "fault_hook", crash)
    with pytest.raises(RuntimeError):
        _reply(db, clock, asked, "APPROVE")
    assert _notices(db) == []


# --- cases and loads with nothing to hang a notice on ----------------------------------------------------------------
def _case(db: sqlite3.Connection, clock: FakeClock, shipment: str | None) -> str:
    return create_handoff(db, HandoffRequest(reason_code="DRIVER_REQUESTED", driver_id="DRV006", thread_id="THR001",
                                             shipment_id=shipment, summary="s", created_at=clock.now()), clock).case_id


def test_a_case_with_no_load_tells_the_driver_when_claimed_and_resolved(db: sqlite3.Connection, clock: FakeClock) -> None:
    case = _case(db, clock, None)
    claim_case(db, case, "asha", clock)
    resolve_case(db, case, "asha", "ADVISED_DRIVER", "go to gate 2", clock)
    assert len(_notices(db)) == 2 and "go to gate 2" in _notices(db)[1]


def test_a_load_with_no_appointment_still_gets_the_notice(db: sqlite3.Connection, clock: FakeClock) -> None:
    bare = db.execute("select s.shipment_id, s.driver_id from shipments s where not exists"
                      " (select 1 from appointments a where a.shipment_id=s.shipment_id) limit 1").fetchone()
    assert bare is not None, "the sample data has a load with no appointment"
    assert queue_driver_notice(db, bare["driver_id"], bare["shipment_id"], "hello", clock.now())
    assert _notices(db, bare["driver_id"]) == ["hello"]


def test_a_failed_notice_with_no_load_can_be_seen_and_resent_by_an_admin_only(db: sqlite3.Connection,
                                                                             clock: FakeClock) -> None:
    admin = add_user(db, clock, "admin@example.test", "ADMIN")
    staff = add_user(db, clock, "jaipur@example.test", "WAREHOUSE_AUTHORITY", ("FAC-JAI-01",))
    queue_driver_notice(db, "DRV006", None, "hello", clock.now())
    db.execute("update notification_outbox set status='PERMANENT_FAILURE'")
    db.commit()
    assert driver_messages.undelivered(db, staff) == {}
    shown = driver_messages.undelivered(db, admin)["DRV006"]
    assert [n["text"] for n in shown] == ["hello"]
    driver_messages.resend(db, actor=admin, outbox_id=shown[0]["outbox_id"], clock=clock)
    assert db.execute("select status from notification_outbox").fetchone()[0] == "PENDING"


# --- holds -------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("message,language", [
    ("SHP1006 is running 60 minutes late", "en"),
    ("bhai meri gaadi 60 minute late hai, SHP1006", "hinglish"),
    ("मेरी गाड़ी 60 मिनट लेट है, SHP1006", "hi"),
])
def test_a_lapsed_hold_is_announced_in_the_drivers_language(db: sqlite3.Connection, clock: FakeClock, tmp_path: Path,
                                                           message: str, language: str) -> None:
    seed_everything_known(db)
    Chat(db, clock, tmp_path).say(message, SEARCH)
    clock.advance(timedelta(minutes=5))
    expire_due_holds(db, clock)
    assert _notices(db) == [say("hold_expired", language)]


def test_a_hold_released_because_the_truck_is_too_far_reaches_the_phone(db: sqlite3.Connection, clock: FakeClock,
                                                                       tmp_path: Path) -> None:
    seed_everything_known(db)
    chat = Chat(db, clock, tmp_path)
    chat.say("SHP1006 is running 60 minutes late", SEARCH)
    store_ping(db, "SHP1006", GpsPing(lat=28.6, lon=77.2, recorded_at=clock.now()), clock, "TEST")
    db.commit()
    handle_new_position(db, "SHP1006", clock, chat.memory)
    assert chat.held() == 0 and len(_notices(db)) == 1 and "D2" in _notices(db)[0]


# --- the migration ---------------------------------------------------------------------------------------------------
def test_migration_023_keeps_the_notices_already_queued(tmp_path: Path) -> None:
    from setuhaul.db import MIGRATIONS_DIR

    before = tmp_path / "before"
    before.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name < "023":
            (before / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path = make_working_db(tmp_path / "w.sqlite3")  # fully migrated, used only for the seed copy
    path.unlink()
    import shutil

    from setuhaul.config import load_settings

    shutil.copyfile(load_settings().db_path, path)
    conn = open_connection(path=path)
    apply_migrations(conn, before)
    appointment, shipment = conn.execute("select appointment_id, shipment_id from appointments limit 1").fetchone()
    conn.execute("insert into appointment_events(appointment_id, shipment_id, event_type, actor_type, occurred_at)"
                 " values (?,?,'X','SYSTEM','t')", (appointment, shipment))
    conn.execute("insert into notification_outbox(dedupe_key, recipient_type, recipient_ref, event_id, payload_json,"
                 " status, attempts, created_at) values ('k','DRIVER','DRV006',1,'{}','FAILED',2,'t')")
    conn.commit()
    assert "023_driver_notices_without_event.sql" in apply_migrations(conn)
    row = conn.execute("select outbox_id, dedupe_key, event_id, status, attempts, shipment_id from notification_outbox").fetchone()
    assert tuple(row) == (1, "k", 1, "FAILED", 2, None)
    conn.close()
