"""Contention scenarios built on the supplied snapshot plus labelled TEST_FIXTURE evidence."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from setuhaul.booking import acquire_hold
from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from setuhaul.proof import Op
from tests.helpers import at, seed_everything_known, show_options

# (shipment, driver): all dock-compatible with any standard dock at FAC-JAI-01
TEN = [("SHP1018", "DRV003"), ("SHP1020", "DRV004"), ("SHP1008", "DRV008"), ("SHP1019", "DRV008"),
       ("SHP1001", "DRV001"), ("SHP1006", "DRV006"), ("SHP1007", "DRV007"), ("SHP1012", "DRV012"),
       ("SHP1013", "DRV013"), ("SHP1017", "DRV001")]


@dataclass
class World:
    ops: list[Op]
    target_slot: str = ""
    refs: dict[str, str] = field(default_factory=dict)


def ensure_thread(conn: sqlite3.Connection, driver_id: str) -> str:
    row = conn.execute("select thread_id from chat_threads where driver_id=? order by thread_id limit 1", (driver_id,)).fetchone()
    if row:
        return str(row[0])
    thread = f"THR-{driver_id}"
    conn.execute("insert into chat_threads (thread_id, driver_id, opened_at, thread_status, thread_intent) "
                 "values (?,?, '2026-08-04T09:00:00+05:30','OPEN','UNKNOWN')", (thread, driver_id))
    conn.commit()
    return thread


def _hold_op(conn: sqlite3.Connection, shipment: str, driver: str, eta: str, clock: FakeClock, now: str, role: str,
             must_include: str | None = None) -> tuple[Op, str]:
    thread = ensure_thread(conn, driver)
    opts = show_options(conn, clock, thread, shipment, eta, limit=20)
    slots = [i.slot_id for i in opts.items]
    return Op("hold", {"driver_id": driver, "thread_id": thread, "shipment_id": shipment,
                       "option_set_id": opts.option_set_id, "rank": 1}, now, role), slots[0] if not must_include else must_include


def contention_world(path: Path, clock: FakeClock, n: int = 10) -> World:
    """``n`` different shipments, all shown the same slot, all asking to hold it at once."""
    conn = open_connection(path=path)
    seed_everything_known(conn)
    sets = []
    for shipment, driver in TEN[:n]:
        thread = ensure_thread(conn, driver)
        sets.append((shipment, driver, thread, show_options(conn, clock, thread, shipment, "13:30", limit=20)))
    common = set.intersection(*[{i.slot_id for i in s[3].items} for s in sets])
    first = sets[0][3]
    target = next(i.slot_id for i in first.items if i.slot_id in common)
    ops = []
    for shipment, driver, thread, opts in sets:
        rank = next(i.rank for i in opts.items if i.slot_id == target)
        ops.append(Op("hold", {"driver_id": driver, "thread_id": thread, "shipment_id": shipment,
                               "option_set_id": opts.option_set_id, "rank": rank}, clock.now().isoformat(), "contender"))
    conn.close()
    return World(ops, target_slot=target)


def mixed_world(path: Path, clock: FakeClock) -> World:
    """Same-slot contention plus release, expiry, confirmation, cancellation and a chained hand-off."""
    conn = open_connection(path=path)
    seed_everything_known(conn)
    t0 = clock.now()
    early = FakeClock(t0)
    late = FakeClock(t0 + timedelta(minutes=2))  # holds made here outlive the sweep below
    refs: dict[str, str] = {}

    def make_hold(clk: FakeClock, shipment: str, driver: str, eta: str) -> str:
        thread = ensure_thread(conn, driver)
        opts = show_options(conn, clk, thread, shipment, eta)
        held = acquire_hold(conn, driver, thread, shipment, opts.option_set_id, 1, clk)
        assert held.status == "HELD", held
        return str(held.appointment_id)

    refs["expire_hold"] = make_hold(early, "SHP1007", "DRV007", "11:00")
    refs["release_hold"] = make_hold(late, "SHP1001", "DRV001", "11:00")
    refs["confirm_hold"] = make_hold(late, "SHP1006", "DRV006", "11:00")
    refs["confirm_original"], refs["confirm_original_slot"] = "APT1006", "SLOT-JAI-045"
    refs["cancel_appointment"] = "APT1004"

    # the chained hand-off: shown the slot that SHP1006's confirmation will free
    conn.execute("update appointments set appointment_status='CANCELLED', is_current=0 where appointment_id='APT1006'")
    conn.commit()
    swap_thread = ensure_thread(conn, "DRV001")
    swap_opts = show_options(conn, late, swap_thread, "SHP1017", "09:45", limit=30)
    conn.execute("update appointments set appointment_status='CONFIRMED', is_current=1 where appointment_id='APT1006'")
    conn.commit()
    swap_rank = next(i.rank for i in swap_opts.items if i.slot_id == "SLOT-JAI-045")

    contenders = []
    sets = []
    for shipment, driver in [("SHP1018", "DRV003"), ("SHP1020", "DRV004"), ("SHP1012", "DRV012"), ("SHP1013", "DRV013")]:
        thread = ensure_thread(conn, driver)
        sets.append((shipment, driver, thread, show_options(conn, late, thread, shipment, "13:30", limit=20)))
    common = set.intersection(*[{i.slot_id for i in s[3].items} for s in sets])
    target = next(i.slot_id for i in sets[0][3].items if i.slot_id in common)
    now = (t0 + timedelta(minutes=3)).isoformat()
    for shipment, driver, thread, opts in sets:
        rank = next(i.rank for i in opts.items if i.slot_id == target)
        contenders.append(Op("hold", {"driver_id": driver, "thread_id": thread, "shipment_id": shipment,
                                      "option_set_id": opts.option_set_id, "rank": rank}, now, "contender"))
    ops = [
        *contenders,
        Op("release", {"driver_id": "DRV001", "thread_id": ensure_thread(conn, "DRV001"),
                       "appointment_id": refs["release_hold"], "reason": "DRIVER_CANCELLED"}, now, "release"),
        Op("sweep", {}, (t0 + timedelta(seconds=181)).isoformat(), "expire"),
        Op("confirm", {"driver_id": "DRV006", "thread_id": "THR001", "message_id": "MSG-MIX", "shipment_id": "SHP1006",
                       "hold_appointment_id": refs["confirm_hold"]}, now, "confirm"),
        Op("cancel", {"driver_id": "DRV004", "thread_id": ensure_thread(conn, "DRV004"), "message_id": "MSG-CAN",
                      "shipment_id": "SHP1004"}, at("06:30").isoformat(), "cancel"),
        Op("hold", {"driver_id": "DRV001", "thread_id": swap_thread, "shipment_id": "SHP1017",
                    "option_set_id": swap_opts.option_set_id, "rank": swap_rank}, now, "swap"),
    ]
    conn.close()
    return World(ops, target_slot=target, refs=refs)
