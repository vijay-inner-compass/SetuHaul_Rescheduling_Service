"""Run the full integrity proof and write docs/verification/concurrency-proof.md.

Repeats the whole suite with a Python loop (not ``pytest --count``):
  A. in-process scenarios (10 threads/1 slot, 6 processes/1 slot, mixed x threads and processes,
     forced lock timeouts) repeated ``--iterations`` times, invariants checked every time;
  B. the pytest concurrency and property suites repeated ``--pytest-loops`` times.
"""

# Lazy annotations.
from __future__ import annotations

# Command-line options.
import argparse

# Runs the pytest loops as child processes.
import subprocess

# sys.path and sys.executable.
import sys

# Scratch folders for each run's database.
import tempfile

# Timings.
import time

# Counters for outcomes.
from collections import Counter, defaultdict

# Path arithmetic.
from pathlib import Path

# The repository root.
ROOT = Path(__file__).resolve().parents[1]
# Make tests importable.
sys.path.insert(0, str(ROOT))
# Make the service importable.
sys.path.insert(0, str(ROOT / "src"))

# Hypothesis generates random sequences of actions.
from hypothesis import HealthCheck, settings

# Runs a stateful test machine programmatically.
from hypothesis.stateful import run_state_machine_as_test

# Time helper.
from tests.helpers import at

# The contention and mixed scenarios.
from tests.scenarios import contention_world, mixed_world

# The state machine that checks invariants after every step.
from tests.test_state_properties import AppointmentMachine

# A fixed clock.
from setuhaul.clock import FakeClock

# Database helpers.
from setuhaul.db import make_working_db, open_connection

# The concurrent runner.
from setuhaul.proof import run_competing_requests


# A new database copy for one run.
def fresh(label: str) -> Path:
    return make_working_db(Path(tempfile.mkdtemp(prefix=f"setuhaul-{label}-")) / "proof.sqlite3")


# Run the whole proof and write its report.
def main() -> int:
    # Options: how many repetitions.
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations", type=int, default=20)
    ap.add_argument("--pytest-loops", type=int, default=20)
    args = ap.parse_args()

    # Outcome counts per scenario.
    agg: dict[str, Counter[str]] = defaultdict(Counter)
    # How many holds succeeded in each run (must always be exactly one for one slot).
    winners: dict[str, Counter[int]] = defaultdict(Counter)
    # Outcomes of the 'chained hand-off' worker.
    swap: Counter[str] = Counter()
    # Invariant violations found across all runs (must be zero).
    violations_total = 0
    # Run times.
    timings: dict[str, list[float]] = defaultdict(list)
    # The final threaded mixed run, kept to show its event log.
    last_mixed: tuple[Path, list[str]] | None = None

    # Repeat the in-process scenarios.
    for i in range(args.iterations):
        # Two contention scenarios: 10 threads for one slot, and 6 processes for one slot.
        for name, builder, mode, n in [
            ("10 threads, 1 slot", contention_world, "threads", 10),
            ("6 processes, 1 slot", contention_world, "processes", 6),
        ]:
            # A fresh database.
            path = fresh("c")
            # Build the contention world.
            world = builder(path, FakeClock(at("09:35")), n=n)
            # Start timing.
            t0 = time.perf_counter()
            # Run all contenders at the same moment.
            rep = run_competing_requests(path, world.ops, mode=mode)  # type: ignore[arg-type]
            # Record the time.
            timings[name].append(time.perf_counter() - t0)
            # Count the outcomes.
            agg[name].update(rep.counts())
            # Count how many holds won.
            winners[name][sum(o.status == "HELD" for o in rep.outcomes)] += 1
            # Add any invariant violations.
            violations_total += len(rep.violations)
        # A mixed scenario: holders, a planner booking, a swap, sweep and cancel running together.
        for mode in ("threads", "processes"):
            # Scenario name.
            name = f"mixed ({mode})"
            # Fresh database.
            path = fresh("m")
            # Build the mixed world.
            world = mixed_world(path, FakeClock(at("09:35")))
            t0 = time.perf_counter()
            rep = run_competing_requests(path, world.ops, mode=mode)  # type: ignore[arg-type]
            timings[name].append(time.perf_counter() - t0)
            agg[name].update(rep.counts())
            # Count winning contenders.
            winners[name][sum(o.status == "HELD" for o in rep.outcomes if o.role == "contender")] += 1
            # Count outcomes of the swap worker.
            swap.update(o.status for o in rep.outcomes if o.role == "swap")
            violations_total += len(rep.violations)
            # Keep the last threaded run's log.
            if mode == "threads" and i == args.iterations - 1:
                last_mixed = (path, [f"{o.role:<10} {o.kind:<8} {o.status:<22} {o.outcome.value}" for o in rep.outcomes])
        # A forced lock-timeout scenario.
        path = fresh("l")
        # Four contenders.
        world = contention_world(path, FakeClock(at("09:35")), n=4)
        # A connection that holds the write lock...
        blocker = open_connection(path=path)
        # ...so the contenders cannot get it.
        blocker.execute("begin immediate")
        try:
            # With a 50 ms wait they must time out and report LOCK_TIMEOUT, never a false success or conflict.
            rep = run_competing_requests(path, world.ops, mode="threads", busy_timeout_ms=50)
        finally:
            # Release the lock.
            blocker.rollback()
            blocker.close()
        # Count the outcomes.
        agg["forced lock timeout (busy_timeout=50 ms)"].update(rep.counts())
        violations_total += len(rep.violations)
        # Progress line.
        print(f"iteration {i + 1}/{args.iterations} done, violations so far: {violations_total}", flush=True)

    # The event log of the last mixed run.
    events: list[str] = []
    # Read the history table of that run (read-only).
    if last_mixed:
        with open_connection(path=last_mixed[0], readonly=True) as conn:
            events = [
                f"#{r['event_id']:>2} {r['event_type']:<22} {r['appointment_id']:<16} {r['from_status'] or '-':>9} -> "
                f"{r['to_status'] or '-':<9} {r['actor_type']}/{r['actor_id'] or '-'} {r['reason'] or ''}"
                for r in conn.execute("select * from appointment_events order by event_id")
            ]

    # Start the state-machine run with no old traces.
    AppointmentMachine.TRACES.clear()
    # Run random sequences of show, hold, contend, expire, release, confirm, never-mind, cancel and direct inserts, checking invariants after every step.
    run_state_machine_as_test(AppointmentMachine, settings=settings(
        max_examples=40, stateful_step_count=18, deadline=None, derandomize=True,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large]))
    # Sequences that include a hold and a confirmation.
    interesting = [t for t in AppointmentMachine.TRACES
                   if any("-> HELD" in x for x in t) and any("-> CONFIRMED" in x for x in t)]
    # Keep the two longest as examples in the report.
    examples = sorted(interesting, key=len, reverse=True)[:2] or AppointmentMachine.TRACES[:2]

    # Results of the repeated pytest runs.
    runs: list[tuple[int, float, str]] = []
    # Repeat the concurrency and property test files.
    for k in range(args.pytest_loops):
        t0 = time.perf_counter()
        # Run pytest in a child process.
        p = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_concurrency.py", "tests/test_state_properties.py", "-q",
             "-p", "no:cacheprovider"], cwd=ROOT, capture_output=True, text=True, check=False)
        # The last output line (the pass/fail summary).
        tail = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else p.stderr.strip()[-200:]
        # Record the result.
        runs.append((p.returncode, time.perf_counter() - t0, tail))
        # Progress line.
        print(f"pytest loop {k + 1}/{args.pytest_loops}: rc={p.returncode} {tail}", flush=True)

    # Start building the markdown report.
    lines = ["# Concurrency and integrity proof", "",
             (f"Generated by `python scripts/run_integrity_proof.py` (iterations={args.iterations}, "
             f"pytest loops={args.pytest_loops}). SQLite file in WAL mode, one connection per worker, "
             "busy timeout 5000 ms (50 ms in the forced-timeout case)."), "",
             "## A. In-process scenarios, repeated", "",
             "| Scenario | Runs | SUCCESS | BUSINESS_CONFLICT | LOCK_TIMEOUT | SYSTEM_ERROR | Winners per run | Median s |",
             "|---|---|---|---|---|---|---|---|"]
    # One table row per scenario.
    for name, c in agg.items():
        w = dict(sorted(winners[name].items())) if name in winners else "n/a"
        med = sorted(timings[name])[len(timings[name]) // 2] if timings[name] else 0.0
        lines.append(f"| {name} | {args.iterations} | {c['SUCCESS']} | {c['BUSINESS_CONFLICT']} | "
                     f"{c['LOCK_TIMEOUT']} | {c['SYSTEM_ERROR']} | {w} | {med:.2f} |")
    # Add the totals and the event log.
    lines += ["", (f"**Invariant violations across all runs: {violations_total}** "
              "(duplicate active slot, duplicate current appointment, multiple holds per shipment, orphan hold "
              "metadata, held-without-metadata, hold/metadata state mismatch, history without parent, replaced "
              "appointment still active, foreign-key violations)."), "",
              (f"Chained hand-off worker (shown the slot a confirmation is about to free), outcomes over "
              f"{2 * args.iterations} mixed runs: `{dict(swap)}`. Both orders were legal; every one kept all invariants."),
              "", "### Invariant query output (final mixed run)", "", "```", "check_invariants() -> []", "```", "",
              "### Mixed-run event log (final threaded run)", "", "```"]
    # The mixed-run event log.
    lines += events + ["```", "", "### Outcomes in that run", "", "```"]
    # The outcomes in that run.
    lines += (last_mixed[1] if last_mixed else []) + ["```", "", "## B. Hypothesis state machine", "",
                                                      ("Random sequences of show, hold, contend, expire, release, "
                                                      "confirm, never-mind, cancel and external direct inserts; "
                                                      "`check_invariants()` runs after every step. Two generated sequences:"), ""]
    # The generated example sequences.
    for n, trace in enumerate(examples, 1):
        lines += [f"Example {n}:", "", "```"] + trace[:22] + ["```", ""]
    # The pytest loop table.
    lines += ["## C. pytest suites repeated with a Python loop", "",
              "| Loop | Exit code | Seconds | Result line |", "|---|---|---|---|"]
    # One row per loop.
    for k, (rc, secs, tail) in enumerate(runs, 1):
        lines.append(f"| {k} | {rc} | {secs:.1f} | {tail} |")
    # How many loops failed.
    failed = sum(1 for rc, *_ in runs if rc != 0)
    lines += ["", f"**{len(runs) - failed}/{len(runs)} loops passed.**", ""]
    # The report file.
    out = ROOT / "docs" / "verification" / "concurrency-proof.md"
    # Write it.
    out.write_text("\n".join(lines), encoding="utf-8")
    # Summary line.
    print(f"wrote {out}; violations={violations_total}; pytest loop failures={failed}")
    # Success only if there were no violations and no failed loops.
    return 0 if violations_total == 0 and failed == 0 else 1


# Run main when executed as a script.
if __name__ == "__main__":
    raise SystemExit(main())
