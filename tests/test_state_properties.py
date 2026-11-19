"""Phase 7: random sequences of show, hold, expire, release, confirm and cancel.

After every action the same invariants used by the contention proof must hold, and no action may
end in SYSTEM_ERROR. Hypothesis shrinks any failing sequence to a minimal reproduction.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import ClassVar

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from setuhaul.booking import (
    ConfirmCommand,
    OutcomeClass,
    acquire_hold,
    confirm_reschedule,
    release_hold,
)
from setuhaul.cancellation import cancel_confirmed_appointment, cancel_pending_change
from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection
from setuhaul.integrity import check_invariants
from setuhaul.jobs.expire_holds import expire_due_holds
from setuhaul.option_history import OptionSet
from tests.helpers import at, seed_everything_known, show_options

ACTORS = [
    ("DRV006", "THR001", "SHP1006"),
    ("DRV006", "THR001", "SHP1021"),
    ("DRV012", "THR002", "SHP1012"),
    ("DRV013", "THR003", "SHP1013"),
    ("DRV007", "THR006", "SHP1007"),
    ("DRV001", "THR011", "SHP1017"),  # confirmed 12:00 slot: far enough away to be cancellable
]
EXTERNAL_SHIPMENTS = ["SHP1018", "SHP1019", "SHP1008"]
ETAS = ["09:50", "11:00", "12:20", "13:30"]
BAD = {OutcomeClass.SYSTEM_ERROR, OutcomeClass.LOCK_TIMEOUT}  # impossible with one writer


class AppointmentMachine(RuleBasedStateMachine):
    TRACES: ClassVar[list[list[str]]] = []

    def __init__(self) -> None:
        super().__init__()
        self.dir = Path(tempfile.mkdtemp(prefix="setuhaul-prop-"))
        self.conn: sqlite3.Connection = open_connection(path=make_working_db(self.dir / "m.sqlite3"))
        seed_everything_known(self.conn)
        self.clock = FakeClock(at("09:35"))
        self.options: list[OptionSet] = []
        self.holds: list[tuple[str, str, str, str]] = []
        self.slots = [r[0] for r in self.conn.execute(
            "select slot_id from appointment_slots where facility_id='FAC-JAI-01' order by slot_id")]
        self.trace: list[str] = []

    def log(self, text: str) -> None:
        self.trace.append(text)

    def check_ok(self, outcome: OutcomeClass, text: str) -> None:
        assert outcome not in BAD, f"{text} -> {outcome}"

    @rule(a=st.integers(0, len(ACTORS) - 1), eta=st.sampled_from(ETAS))
    def show(self, a: int, eta: str) -> None:
        _driver, thread, shipment = ACTORS[a]
        opts = show_options(self.conn, self.clock, thread, shipment, eta)
        self.options.append(opts)
        self.log(f"show {shipment} eta={eta} -> {len(opts.items)} options")

    @rule(o=st.integers(0, 50), rank=st.integers(1, 3))
    def hold(self, o: int, rank: int) -> None:
        if not self.options:
            return
        s = self.options[o % len(self.options)]
        r = acquire_hold(self.conn, s.driver_id, s.thread_id, s.shipment_id, s.option_set_id, rank, self.clock)
        self.check_ok(r.outcome, f"hold {s.shipment_id}")
        if r.status == "HELD":
            self.holds.append((s.driver_id, s.thread_id, s.shipment_id, str(r.appointment_id)))
        self.log(f"hold {s.shipment_id} rank={rank} -> {r.status}")

    @rule(a=st.integers(0, len(ACTORS) - 1), b=st.integers(0, len(ACTORS) - 1), eta=st.sampled_from(ETAS))
    def contend(self, a: int, b: int, eta: str) -> None:
        """Two shipments are shown the same options, then both ask for rank 1: the second must lose."""
        if ACTORS[a][2] == ACTORS[b][2]:
            return
        sets = [show_options(self.conn, self.clock, ACTORS[i][1], ACTORS[i][2], eta) for i in (a, b)]
        self.options.extend(sets)
        results = []
        for s in sets:
            if not s.items:
                continue
            r = acquire_hold(self.conn, s.driver_id, s.thread_id, s.shipment_id, s.option_set_id, 1, self.clock)
            self.check_ok(r.outcome, f"contend {s.shipment_id}")
            results.append(r.status)
            if r.status == "HELD":
                self.holds.append((s.driver_id, s.thread_id, s.shipment_id, str(r.appointment_id)))
        self.log(f"contend {ACTORS[a][2]} vs {ACTORS[b][2]} eta={eta} -> {results}")

    @rule(seconds=st.sampled_from([30, 120, 181, 900]))
    def advance(self, seconds: int) -> None:
        self.clock.advance(timedelta(seconds=seconds))
        self.log(f"advance {seconds}s")

    @rule()
    def lapse_then_sweep(self) -> None:
        """Let every live hold run past its TTL, then run the sweep job."""
        self.clock.advance(timedelta(seconds=181))
        n = expire_due_holds(self.conn, self.clock).expired
        self.log(f"lapse 181s then expire sweep -> {n}")

    @rule()
    def expire(self) -> None:
        n = expire_due_holds(self.conn, self.clock).expired
        self.log(f"expire sweep -> {n}")

    @rule(h=st.integers(0, 50))
    def release(self, h: int) -> None:
        if not self.holds:
            return
        d, t, s, appt = self.holds[h % len(self.holds)]
        r = release_hold(self.conn, d, t, appt, "DRIVER_CANCELLED", self.clock)
        self.log(f"release {s} -> {r.status}")

    @rule(h=st.integers(0, 50), m=st.integers(0, 2))
    def confirm(self, h: int, m: int) -> None:
        if not self.holds:
            return
        d, t, s, appt = self.holds[h % len(self.holds)]
        r = confirm_reschedule(self.conn, ConfirmCommand(driver_id=d, thread_id=t, message_id=f"MSG-{m}",
                                                         shipment_id=s, hold_appointment_id=appt), self.clock)
        self.check_ok(r.outcome, f"confirm {s}")
        self.log(f"confirm {s} msg={m} -> {r.status}")

    @rule(a=st.integers(0, len(ACTORS) - 1))
    def never_mind(self, a: int) -> None:
        d, t, s = ACTORS[a]
        r = cancel_pending_change(self.conn, d, t, s, self.clock)
        self.log(f"never-mind {s} -> {r.status}")

    @rule(a=st.integers(0, len(ACTORS) - 1), m=st.integers(0, 2))
    def cancel(self, a: int, m: int) -> None:
        d, t, s = ACTORS[a]
        r = cancel_confirmed_appointment(self.conn, d, t, f"CAN-{m}", s, self.clock)
        self.check_ok(r.outcome, f"cancel {s}")
        self.log(f"cancel {s} msg={m} -> {r.status}")

    @rule(x=st.integers(0, 2), slot=st.integers(0, 200))
    def external_writer(self, x: int, slot: int) -> None:
        """A planner or scheduling tool inserting straight into the table."""
        slot_id = self.slots[slot % len(self.slots)]
        stamp = self.clock.now().isoformat()
        try:
            with self.conn:
                self.conn.execute(
                    "insert into appointments (appointment_id, shipment_id, slot_id, appointment_status, "
                    "booking_source, is_current, booked_at, updated_at) values (?,?,?, 'CONFIRMED','PLANNER',1,?,?)",
                    (f"EXT-{len(self.trace)}", EXTERNAL_SHIPMENTS[x], slot_id, stamp, stamp),
                )
            self.log(f"external insert {EXTERNAL_SHIPMENTS[x]} into {slot_id} -> accepted")
        except sqlite3.IntegrityError:
            self.log(f"external insert {EXTERNAL_SHIPMENTS[x]} into {slot_id} -> rejected by constraint")

    @invariant()
    def invariants_hold(self) -> None:
        violations = check_invariants(self.conn)
        assert violations == [], violations

    def teardown(self) -> None:
        type(self).TRACES.append(list(self.trace))
        self.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)


TestAppointmentStateMachine = AppointmentMachine.TestCase
TestAppointmentStateMachine.settings = settings(
    max_examples=30, stateful_step_count=18, deadline=None, derandomize=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


def test_machine_actually_exercises_every_kind_of_action() -> None:
    """Guard against a vacuous property test: the generated runs must reach the interesting paths."""
    from hypothesis.stateful import run_state_machine_as_test

    AppointmentMachine.TRACES.clear()
    run_state_machine_as_test(AppointmentMachine, settings=settings(
        max_examples=40, stateful_step_count=25, deadline=None, derandomize=True,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large]))
    seen = "\n".join(line for trace in AppointmentMachine.TRACES for line in trace)
    for needle in ("-> HELD", "SLOT_CONFLICT", "-> CONFIRMED", "expire sweep -> 1", "-> RELEASED",
                   "rejected by constraint", "-> CANCELLED"):
        assert needle in seen, f"never reached: {needle}"
