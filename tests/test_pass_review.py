"""Findings from the security review of the Dock Access Pass: blanking, and what may happen once a truck is on site."""

from __future__ import annotations

import random
import uuid
from pathlib import Path

import pytest

from setuhaul import facility_change, gate, passes
from setuhaul.booking import ConfirmCommand, acquire_hold, confirm_reschedule
from setuhaul.cancellation import cancel_confirmed_appointment
from setuhaul.db import open_connection
from setuhaul.db import parse_operational_timestamp as ts
from setuhaul.dock_pass import blank, find, guesses, new_code, normalise
from setuhaul.identity import AuthError
from setuhaul.partner_booking import request_slot
from tests.gate_helpers import ARRIVE, Yard
from tests.shipment_helpers import DAY, count, query, slot_at

BODY = "K7QMD-2XABC-PL3RT-Q4ZNV-HJ6WSA"  # one code, with digits, for the shape tests
G = BODY.split("-")


def _leaks(text: str, code: str) -> bool:
    # True if any eight-character stretch of the code survives blanking.
    kept = normalise(blank(text))
    body = normalise(code)[-26:]
    return any(body[i:i + 8] in kept for i in range(len(body) - 7))


@pytest.mark.parametrize("text", [
    "DAP-" + "\n".join(G), "\n".join(G), "DAP  " + "  ".join(G), "DAP\t" + "\t".join(G), "DAP," + ",".join(G), "/".join(G),
    "DAP." + ".".join(G), "_".join(G), "DAP–" + "–".join(G), "−".join(G), "DAP－" + "－".join(G), "DAP " + " ".join(G),
    "DAP-" + "\u200b-".join(G), "".join(G)[:25], "-".join(G[:4]), " ".join(G[:4]), "-".join([G[0], G[1][:4] + "0", *G[2:]]),
    " ".join(["1" + G[0][1:], *G[1:]]), "DAP-" + G[0], "DAP-" + G[0] + "-" + G[1],
    "".join(chr(ord(c) + 0xFEE0) for c in "DAP-" + BODY), "`DAP-" + BODY + "`", "**" + "DAP-" + BODY + "**", "my pass:" + BODY + ".",
])
def test_every_way_of_typing_it_is_blanked(text: str) -> None:
    shown = len(normalise(blank(text)))
    assert "[HIDDEN]" in normalise(blank(text)) and shown < 80
    if len(normalise(text)) >= 8:
        assert not _leaks(text, "DAP-" + BODY)


def test_random_mangling_never_leaks() -> None:
    rng = random.Random(7)
    separators = ["-", " ", "", "\n", "  ", ", ", ".", "_", "/", "–", " "]
    for _ in range(600):
        code = new_code()
        parts = code[4:].split("-")
        if rng.random() < 0.4:
            i = rng.randrange(len(parts))
            parts[i] = parts[i][:-1] + rng.choice("0189")
        if rng.random() < 0.3:
            parts = parts[:rng.choice((4, 5))]
        text = rng.choice(["", "DAP-", "dap ", "here: "]) + rng.choice(separators[:3] + [rng.choice(separators)]).join(parts)
        if rng.random() < 0.5:
            text = text.lower()
        # A damaged code with no digit and no "DAP-" reads like words; that is the one shape not promised.
        if not any(c.isdigit() for part in parts for c in part) and not text.upper().startswith("DAP-"):
            continue
        kept = normalise(blank(text))
        # No two whole groups of the code survive together.
        assert not any(normalise(parts[i] + parts[i + 1]) in kept for i in range(len(parts) - 1)), repr(text)


@pytest.mark.parametrize("text", ["HELLO WORLD THERE THING STUFF2", "I will dap in at 5", "DAP price is high today friend",
                                  "reach by 5 pm, truck RJ14GT4101, order ORD-261127-001, phone +91 98111 00001"])
def test_ordinary_text_is_never_a_guess(text: str) -> None:
    assert guesses(text) == []


def test_ordinary_sentences_are_left_alone() -> None:
    for text in ("I will dap in at 5", "DAP price is high today friend", "my truck broke down near the highway",
                 "reach by 5 pm, truck RJ14GT4101, order ORD-261127-001, phone +91 98111 00001"):
        assert blank(text) == text and find(text) == []


def test_a_code_with_odd_separators_is_still_the_same_code() -> None:
    code = new_code()
    for typed in (code.replace("-", "\n"), code.replace("-", "–"), "".join(chr(ord(c) + 0xFEE0) for c in code),
                  code.replace("-", ", ")):
        assert find(typed) == [normalise(code)], repr(typed)


# ---- once the truck is on site -------------------------------------------------------------------
@pytest.fixture()
def yard(tmp_path: Path) -> Yard:
    return Yard(tmp_path)


def _db(yard: Yard):  # type: ignore[no-untyped-def]
    return open_connection(path=yard.world.path)


def test_a_pass_cannot_be_revoked_under_a_truck_that_is_inside(yard: Yard) -> None:
    yard.call("check_in")
    conn = _db(yard)
    with pytest.raises(AuthError, match="VISIT_IN_PROGRESS"):
        passes.revoke(conn, actor=yard.world.users["wh_a"], shipment_id=yard.shipment_id, reason="lost", clock=yard.world.clock)
    # Even a pass revoked by other means leaves the truck listed and swept.
    query(yard.world, "update dock_access_authorizations set status='REVOKED'")
    assert len(gate.inside(conn, yard.world.users["guard"])) == 1
    yard.world.clock.set(ts(f"{DAY}T18:00:00+05:30"))  # type: ignore[attr-defined]
    assert gate.sweep(conn, yard.world.clock) == 1
    conn.close()


@pytest.mark.parametrize("unloaded", [False, True])
def test_the_booking_is_frozen_once_on_site(yard: Yard, unloaded: bool) -> None:
    query(yard.world, "insert or ignore into chat_threads(thread_id, driver_id, opened_at, thread_status, thread_intent) "
                      "values ('THR-R','DRV001','2026-11-28T10:00:00+05:30','OPEN','UNKNOWN')")
    held = _hold(yard)
    visit = yard.call("check_in").truck_visit_id
    if unloaded:
        yard.call("unloading_complete", "wh_a", truck_visit_id=visit)
    conn = _db(yard)
    done = confirm_reschedule(conn, ConfirmCommand(driver_id="DRV001", thread_id="THR-R", shipment_id=yard.shipment_id,
                                                   hold_appointment_id=held, message_id=uuid.uuid4().hex), yard.world.clock)
    assert done.status != "CONFIRMED"
    cancelled = cancel_confirmed_appointment(conn, "DRV001", "THR-R", uuid.uuid4().hex, yard.shipment_id, yard.world.clock)
    assert cancelled.status in ("NOT_CANCELLABLE", "NOT_FOUND")
    moved = facility_change.request_change(conn, yard.world.clock, driver_id="DRV001", shipment_id=yard.shipment_id,
                                           to_facility_id="FAC-GGN-01")
    assert moved.status == "NOT_MOVABLE"
    start = yard.world.clock.now().replace(hour=20)
    asked = request_slot(conn, yard.world.clock, shipment_id=yard.shipment_id, proposed_start=start,
                         proposed_end=start.replace(hour=21))
    assert asked.status == "SHIPMENT_NOT_RESCHEDULABLE"
    conn.close()
    assert count(yard.world, "appointments", f"shipment_id='{yard.shipment_id}' and is_current=1") == 1


def test_no_new_hold_once_on_site(yard: Yard) -> None:
    query(yard.world, "insert or ignore into chat_threads(thread_id, driver_id, opened_at, thread_status, thread_intent) "
                      "values ('THR-R','DRV001','2026-11-28T10:00:00+05:30','OPEN','UNKNOWN')")
    yard.call("check_in")
    assert _hold(yard, expect=None) != "HELD"


def _hold(yard: Yard, expect: str | None = "HELD") -> str:
    from setuhaul.allocation import rank_options
    from setuhaul.capacity import build_facility_state
    from setuhaul.feasibility import build_context, search_feasible_slots
    from setuhaul.option_history import record_option_set
    from setuhaul.schemas import BaselineType, EtaEvidence

    conn = _db(yard)
    try:
        query(yard.world, "update shipment_assignments set confirmed_at=coalesce(confirmed_at,'2026-11-28T10:00:00+05:30')")
        eta = EtaEvidence(eta=ts(f"{DAY}T15:00:00+05:30"), source="DRIVER_DECLARED", confidence="HIGH",
                          baseline_type=BaselineType.PLANNED_ETA)
        now = yard.world.clock.now()
        context = build_context(conn, yard.shipment_id, eta, now)
        state = build_facility_state(conn, context.facility_id, now)
        shown = record_option_set(conn, context, "THR-R", rank_options(search_feasible_slots(context, state).feasible, limit=3),
                                  state, yard.world.clock)
        got = acquire_hold(conn, "DRV001", "THR-R", yard.shipment_id, shown.option_set_id, 1, yard.world.clock)
        if expect is None:
            return str(got.status)
        assert got.status == expect, got
        return str(got.appointment_id)
    finally:
        conn.close()


def test_harmless_refusals_do_not_lock_the_guard_out(tmp_path: Path) -> None:
    yard = Yard(tmp_path, arrive=False)
    for _ in range(15):
        with pytest.raises(AuthError, match="TOO_EARLY"):
            yard.call("check_in")
    yard.world.clock.set(ts(ARRIVE))  # type: ignore[attr-defined]
    assert yard.call("check_in").order_reference


def test_rebooking_after_the_window_passed_revives_the_pass(tmp_path: Path) -> None:
    yard = Yard(tmp_path, arrive=False)
    query(yard.world, "update dock_access_authorizations set status='EXPIRED'")
    query(yard.world, "update appointments set slot_id=? where shipment_id=?", slot_at(yard.world, 15), yard.shipment_id)
    conn = _db(yard)
    passes.sync_window(conn, yard.shipment_id, yard.world.clock)
    conn.commit()
    conn.close()
    row = yard.one("select status, valid_from_utc from dock_access_authorizations")
    assert tuple(row) == ("ISSUED", "2026-11-28T08:30:00+00:00")
    yard.world.clock.set(ts(f"{DAY}T14:30:00+05:30"))  # type: ignore[attr-defined]
    assert yard.call("validate").order_reference
