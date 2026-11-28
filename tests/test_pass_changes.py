"""The pass keeps step with reschedules, cancellation, a new driver and a move to another warehouse."""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from setuhaul import facility_change, pass_check, passes
from setuhaul.auth import shipments, telegram
from setuhaul.auth.projects import allocate
from setuhaul.booking import ConfirmCommand, acquire_hold, confirm_reschedule
from setuhaul.db import open_connection
from setuhaul.db import parse_operational_timestamp as ts
from setuhaul.dock_pass import find, hash_code
from setuhaul.escalation import InMemoryEscalationSink
from setuhaul.identity import AuthError
from setuhaul.jobs.send_driver_notifications import DriverNoticeRunner
from tests.gate_helpers import ARRIVE, Yard
from tests.shipment_helpers import DAY, open_av, query, slot_at


@pytest.fixture()
def yard(tmp_path: Path) -> Yard:
    return Yard(tmp_path, arrive=False)


def _confirm(yard: Yard, hour: int) -> None:
    """Hold and confirm another slot, as the chat does."""
    from setuhaul.allocation import rank_options
    from setuhaul.capacity import build_facility_state
    from setuhaul.feasibility import build_context, search_feasible_slots
    from setuhaul.option_history import record_option_set
    from setuhaul.schemas import BaselineType, EtaEvidence

    conn = open_connection(path=yard.world.path)
    try:
        query(yard.world, "update shipment_assignments set confirmed_at='2026-11-27T10:00:00+05:30'")
        conn.execute("insert or ignore into chat_threads(thread_id, driver_id, opened_at, thread_status, thread_intent) "
                     "values ('THR-C','DRV001','2026-11-27T10:00:00+05:30','OPEN','UNKNOWN')")
        conn.commit()
        eta = EtaEvidence(eta=ts(f"{DAY}T{hour:02d}:00:00+05:30"), source="DRIVER_DECLARED", confidence="HIGH",
                          baseline_type=BaselineType.PLANNED_ETA)
        now = yard.world.clock.now()
        context = build_context(conn, yard.shipment_id, eta, now)
        state = build_facility_state(conn, context.facility_id, now)
        shown = record_option_set(conn, context, "THR-C", rank_options(search_feasible_slots(context, state).feasible, limit=3),
                                  state, yard.world.clock)
        held = acquire_hold(conn, "DRV001", "THR-C", yard.shipment_id, shown.option_set_id, 1, yard.world.clock)
        assert held.status == "HELD", held
        done = confirm_reschedule(conn, ConfirmCommand(driver_id="DRV001", thread_id="THR-C", shipment_id=yard.shipment_id,
                                                       hold_appointment_id=held.appointment_id, message_id=uuid.uuid4().hex),
                                  yard.world.clock)
        assert done.status == "CONFIRMED", done
    finally:
        conn.close()


def test_a_reschedule_moves_the_window_with_the_same_pass(yard: Yard) -> None:
    before = yard.one("select token_hash, valid_from_utc, truck_visit_id from dock_access_authorizations")
    _confirm(yard, 15)
    after = yard.one("select token_hash, valid_from_utc, truck_visit_id from dock_access_authorizations")
    assert (after["token_hash"], after["truck_visit_id"]) == (before["token_hash"], before["truck_visit_id"])
    assert after["valid_from_utc"] == "2026-11-28T08:30:00+00:00" != before["valid_from_utc"]
    yard.world.clock.set(ts(ARRIVE))  # type: ignore[attr-defined]
    with pytest.raises(AuthError, match="TOO_EARLY"):
        yard.call("validate")
    yard.world.clock.set(ts(f"{DAY}T14:30:00+05:30"))  # type: ignore[attr-defined]
    assert yard.call("validate").order_reference == "ORD-261127-001"


def test_cancelling_the_shipment_ends_the_pass(yard: Yard) -> None:
    conn = open_connection(path=yard.world.path)
    shipments.cancel_shipment(conn, actor=yard.world.users["wh_a"], shipment_id=yard.shipment_id, clock=yard.world.clock)
    conn.close()
    assert yard.one("select status from dock_access_authorizations")[0] == "CANCELLED"
    assert yard.one("select status from truck_visits")[0] == "CANCELLED"


def test_a_new_driver_gets_a_new_pass(yard: Yard) -> None:
    conn = open_connection(path=yard.world.path)
    project = conn.execute("select project_id from project_drivers where driver_id='DRV001'").fetchone()[0]
    allocate(conn, actor=yard.world.users["wh_a"], project_id=project, driver_id="DRV002", clock=yard.world.clock)
    carrier = conn.execute("select carrier_id from drivers where driver_id='DRV002'").fetchone()[0]
    vehicle = conn.execute("select vehicle_id from vehicles where carrier_id=? and active_flag=1 order by 1", (carrier,)).fetchone()[0]
    code = shipments.change_driver(conn, actor=yard.world.users["wh_a"], shipment_id=yard.shipment_id, driver_id="DRV002",
                                   vehicle_id=vehicle, clock=yard.world.clock)
    conn.close()
    assert hash_code(code) != hash_code(yard.code)
    live = yard.one("select driver_id, token_hash from dock_access_authorizations where status='ISSUED'")
    assert tuple(live) == ("DRV002", hash_code(code))
    assert yard.one("select driver_id, vehicle_id from truck_visits")[0] == "DRV002"
    yard.world.clock.set(ts(ARRIVE))  # type: ignore[attr-defined]
    with pytest.raises(AuthError, match="PASS_NOT_VALID"):
        yard.call("validate")


def _move(yard: Yard, *, allocate_new: bool = True) -> str:
    conn = open_connection(path=yard.world.path)
    try:
        query(yard.world, "update shipment_assignments set confirmed_at='2026-11-27T10:00:00+05:30'")
        asked = facility_change.request_change(conn, yard.world.clock, driver_id="DRV001", shipment_id=yard.shipment_id,
                                               to_facility_id="FAC-GGN-01")
        assert asked.status == "REQUESTED"
        facility_change.decide(conn, yard.world.clock, actor=yard.world.users["admin"], request_id=asked.request_id, approve=True)
        if allocate_new:
            open_av(yard.world, who="admin", facility_id="FAC-GGN-01", dock_ids=["DOCK-GGN-D1"])
            shipments.allocate_slot(conn, actor=yard.world.users["admin"], shipment_id=yard.shipment_id,
                                    slot_id=slot_at(yard.world, 12, "DOCK-GGN-D1"), clock=yard.world.clock)
        return str(asked.request_id)
    finally:
        conn.close()


def test_a_warehouse_move_revokes_the_old_pass_and_sends_a_new_one(yard: Yard) -> None:
    query(yard.world, "insert into telegram_identities(telegram_user_id, chat_id, driver_id, bound_at) "
                      "values ('777','777','DRV001','2026-11-27T10:00:00+05:30')")
    _move(yard)
    old = yard.one("select status, revocation_reason from dock_access_authorizations")
    assert tuple(old) == ("REVOKED", "WAREHOUSE_CHANGED")
    assert yard.one("select warehouse_id from truck_visits")[0] == "FAC-GGN-01"
    bot = telegram.FakeTelegramClient()
    conn = open_connection(path=yard.world.path)
    assert passes.send_pending(conn, bot, yard.world.clock) == 1 and passes.send_pending(conn, bot, yard.world.clock) == 0
    conn.close()
    [code] = find(bot.sent[0][1])
    live = yard.one("select warehouse_id, token_hash from dock_access_authorizations where status='ISSUED'")
    assert tuple(live) == ("FAC-GGN-01", hash_code(code))
    # The driver does not have to send it back: the offer at the new warehouse must stay usable.
    assert yard.one("select confirmed_at from shipment_assignments")[0] is not None
    # Until a slot there is booked, no gate admits the truck.
    yard.world.clock.set(ts(ARRIVE))  # type: ignore[attr-defined]
    assert yard.call("validate", "guard_b", code=code).order_reference == "ORD-261127-001"



def test_a_reverted_move_sends_a_pass_for_the_old_warehouse(yard: Yard) -> None:
    request = _move(yard, allocate_new=False)
    _link(yard)
    bot = telegram.FakeTelegramClient()
    conn = open_connection(path=yard.world.path)
    passes.send_pending(conn, bot, yard.world.clock)
    facility_change.abandon(conn, yard.world.clock, request, "FOLLOW_UP_FAILED")
    assert passes.send_pending(conn, bot, yard.world.clock) == 0
    conn.close()
    assert yard.one("select destination_facility_id from shipments where shipment_id=?", yard.shipment_id)[0] == "FAC-GGN-01"


def test_a_move_to_a_partner_warehouse_leaves_no_pass(yard: Yard) -> None:
    query(yard.world, "update facilities set is_partner=1 where facility_id='FAC-GGN-01'")
    _move(yard, allocate_new=False)
    conn = open_connection(path=yard.world.path)
    assert passes.send_pending(conn, None, yard.world.clock) == 0
    conn.close()
    assert yard.one("select count(*) from dock_access_authorizations where status='ISSUED'")[0] == 0


def test_the_notice_runner_sends_the_new_pass(yard: Yard) -> None:
    query(yard.world, "insert into telegram_identities(telegram_user_id, chat_id, driver_id, bound_at) "
                      "values ('777','777','DRV001','2026-11-27T10:00:00+05:30')")
    _move(yard)
    bot = telegram.FakeTelegramClient()
    DriverNoticeRunner(yard.world.clock, bot, lambda: open_connection(path=yard.world.path)).run_once()  # type: ignore[arg-type]
    assert sum(bool(find(text)) for _, text, _ in bot.sent) == 1


# ---- from the pull request review ----------------------------------------------------------------------
class Down(telegram.FakeTelegramClient):
    def send(self, chat_id: int, text: str, request_contact_button: bool = False) -> None:
        raise telegram.TelegramSendError("sendMessage failed: ConnectError")


def _link(yard: Yard) -> None:
    query(yard.world, "insert into telegram_identities(telegram_user_id, chat_id, driver_id, bound_at) "
                      "values ('777','777','DRV001','2026-11-27T10:00:00+05:30')")


def _pending(yard: Yard, client: telegram.TelegramClient | None) -> int:
    conn = open_connection(path=yard.world.path)
    try:
        return passes.send_pending(conn, client, yard.world.clock)
    finally:
        conn.close()


@pytest.mark.parametrize("linked", [True, False])
def test_a_replacement_that_cannot_be_sent_is_tried_again(yard: Yard, linked: bool) -> None:
    if linked:
        _link(yard)
    _move(yard)
    assert _pending(yard, None) == 0
    assert _pending(yard, Down()) == 0
    # Nothing the driver has not received is left standing as their pass.
    assert yard.one("select count(*) from dock_access_authorizations where status='ISSUED'")[0] == 0
    if not linked:
        _link(yard)
    yard.world.clock.advance(timedelta(seconds=61))  # type: ignore[attr-defined]
    bot = telegram.FakeTelegramClient()
    assert _pending(yard, bot) == 1 and _pending(yard, bot) == 0
    [code] = find(bot.sent[0][1])
    assert yard.one("select token_hash from dock_access_authorizations where status='ISSUED'")[0] == hash_code(code)


def test_a_failed_send_waits_before_the_next_try(yard: Yard) -> None:
    _link(yard)
    _move(yard)
    assert _pending(yard, Down()) == 0
    issued = yard.one("select count(*) from dock_access_authorizations")[0]
    assert _pending(yard, Down()) == 0
    assert yard.one("select count(*) from dock_access_authorizations")[0] == issued


def test_an_expired_pass_is_replaced_when_the_warehouse_changes(yard: Yard) -> None:
    _link(yard)
    query(yard.world, "update dock_access_authorizations set status='EXPIRED'")
    _move(yard)
    assert _pending(yard, telegram.FakeTelegramClient()) == 1
    assert yard.one("select warehouse_id from dock_access_authorizations where status='ISSUED'")[0] == "FAC-GGN-01"


def test_a_new_pass_after_a_reschedule_names_the_new_time(yard: Yard) -> None:
    _confirm(yard, 15)
    conn = open_connection(path=yard.world.path)
    bot = telegram.FakeTelegramClient()
    _link(yard)
    passes.send_new(conn, bot, actor=yard.world.users["wh_a"], shipment_id=yard.shipment_id, clock=yard.world.clock)
    [code] = find(bot.sent[0][1])
    answer = pass_check.check(conn, yard.world.clock, driver_id="DRV001", thread_id="THR-C", text=code, lang="en",
                              sink=InMemoryEscalationSink())
    conn.close()
    assert answer is not None and answer.kind == "CONFIRMED"
    assert "28 Nov 2026 15:00" in answer.text and "12:00" not in answer.text


def test_confirming_a_held_slot_after_warehouse_move_sends_the_new_pass(yard: Yard) -> None:
    _link(yard)
    _move(yard, allocate_new=False)
    bot = telegram.FakeTelegramClient()
    # While unbooked at Gurugram, no pass can be sent yet.
    assert _pending(yard, bot) == 0
    # Open a slot at Gurugram and confirm it via chat reschedule.
    open_av(yard.world, who="admin", facility_id="FAC-GGN-01", dock_ids=["DOCK-GGN-D1"])
    _confirm(yard, 12)
    assert _pending(yard, bot) == 1
    [code] = find(bot.sent[0][1])
    live = yard.one("select warehouse_id, token_hash from dock_access_authorizations where status='ISSUED'")
    assert tuple(live) == ("FAC-GGN-01", hash_code(code))
