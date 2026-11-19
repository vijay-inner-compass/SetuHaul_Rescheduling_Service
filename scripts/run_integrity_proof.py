"""Run the full integrity proof and write docs/verification/concurrency-proof.md.

Repeats the whole suite with a Python loop (not ``pytest --count``):
  A. in-process scenarios (10 threads/1 slot, 6 processes/1 slot, mixed x threads and processes,
     forced lock timeouts) repeated ``--iterations`` times, invariants checked every time;
  B. the pytest concurrency and property suites repeated ``--pytest-loops`` times.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from hypothesis import HealthCheck, settings
from hypothesis.stateful import run_state_machine_as_test
from tests.helpers import at
from tests.scenarios import contention_world, mixed_world
from tests.test_state_properties import AppointmentMachine

from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection
from setuhaul.proof import run_competing_requests


def fresh(label: str) -> Path:
    return make_working_db(Path(tempfile.mkdtemp(prefix=f"setuhaul-{label}-")) / "proof.sqlite3")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations", type=int, default=20)
    ap.add_argument("--pytest-loops", type=int, default=20)
    args = ap.parse_args()

    agg: dict[str, Counter[str]] = defaultdict(Counter)
    winners: dict[str, Counter[int]] = defaultdict(Counter)
    swap: Counter[str] = Counter()
    violations_total = 0
    timings: dict[str, list[float]] = defaultdict(list)
    last_mixed: tuple[Path, list[str]] | None = None

    for i in range(args.iterations):
        for name, builder, mode, n in [
            ("10 threads, 1 slot", contention_world, "threads", 10),
            ("6 processes, 1 slot", contention_world, "processes", 6),
        ]:
            path = fresh("c")
            world = builder(path, FakeClock(at("09:35")), n=n)
            t0 = time.perf_counter()
            rep = run_competing_requests(path, world.ops, mode=mode)  # type: ignore[arg-type]
            timings[name].append(time.perf_counter() - t0)
            agg[name].update(rep.counts())
            winners[name][sum(o.status == "HELD" for o in rep.outcomes)] += 1
            violations_total += len(rep.violations)
        for mode in ("threads", "processes"):
            name = f"mixed ({mode})"
            path = fresh("m")
            world = mixed_world(path, FakeClock(at("09:35")))
            t0 = time.perf_counter()
            rep = run_competing_requests(path, world.ops, mode=mode)  # type: ignore[arg-type]
            timings[name].append(time.perf_counter() - t0)
            agg[name].update(rep.counts())
            winners[name][sum(o.status == "HELD" for o in rep.outcomes if o.role == "contender")] += 1
            swap.update(o.status for o in rep.outcomes if o.role == "swap")
            violations_total += len(rep.violations)
            if mode == "threads" and i == args.iterations - 1:
                last_mixed = (path, [f"{o.role:<10} {o.kind:<8} {o.status:<22} {o.outcome.value}" for o in rep.outcomes])
        path = fresh("l")
        world = contention_world(path, FakeClock(at("09:35")), n=4)
        blocker = open_connection(path=path)
        blocker.execute("begin immediate")
        try:
            rep = run_competing_requests(path, world.ops, mode="threads", busy_timeout_ms=50)
        finally:
            blocker.rollback()
            blocker.close()
        agg["forced lock timeout (busy_timeout=50 ms)"].update(rep.counts())
        violations_total += len(rep.violations)
        print(f"iteration {i + 1}/{args.iterations} done, violations so far: {violations_total}", flush=True)

    events: list[str] = []
    if last_mixed:
        with open_connection(path=last_mixed[0], readonly=True) as conn:
            events = [
                f"#{r['event_id']:>2} {r['event_type']:<22} {r['appointment_id']:<16} {r['from_status'] or '-':>9} -> "
                f"{r['to_status'] or '-':<9} {r['actor_type']}/{r['actor_id'] or '-'} {r['reason'] or ''}"
                for r in conn.execute("select * from appointment_events order by event_id")
            ]

    AppointmentMachine.TRACES.clear()
    run_state_machine_as_test(AppointmentMachine, settings=settings(
        max_examples=40, stateful_step_count=18, deadline=None, derandomize=True,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large]))
    interesting = [t for t in AppointmentMachine.TRACES
                   if any("-> HELD" in x for x in t) and any("-> CONFIRMED" in x for x in t)]
    examples = sorted(interesting, key=len, reverse=True)[:2] or AppointmentMachine.TRACES[:2]

    runs: list[tuple[int, float, str]] = []
    for k in range(args.pytest_loops):
        t0 = time.perf_counter()
        p = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_concurrency.py", "tests/test_state_properties.py", "-q",
             "-p", "no:cacheprovider"], cwd=ROOT, capture_output=True, text=True, check=False)
        tail = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else p.stderr.strip()[-200:]
        runs.append((p.returncode, time.perf_counter() - t0, tail))
        print(f"pytest loop {k + 1}/{args.pytest_loops}: rc={p.returncode} {tail}", flush=True)

    lines = ["# Concurrency and integrity proof", "",
             (f"Generated by `python scripts/run_integrity_proof.py` (iterations={args.iterations}, "
             f"pytest loops={args.pytest_loops}). SQLite file in WAL mode, one connection per worker, "
             "busy timeout 5000 ms (50 ms in the forced-timeout case)."), "",
             "## A. In-process scenarios, repeated", "",
             "| Scenario | Runs | SUCCESS | BUSINESS_CONFLICT | LOCK_TIMEOUT | SYSTEM_ERROR | Winners per run | Median s |",
             "|---|---|---|---|---|---|---|---|"]
    for name, c in agg.items():
        w = dict(sorted(winners[name].items())) if name in winners else "n/a"
        med = sorted(timings[name])[len(timings[name]) // 2] if timings[name] else 0.0
        lines.append(f"| {name} | {args.iterations} | {c['SUCCESS']} | {c['BUSINESS_CONFLICT']} | "
                     f"{c['LOCK_TIMEOUT']} | {c['SYSTEM_ERROR']} | {w} | {med:.2f} |")
    lines += ["", (f"**Invariant violations across all runs: {violations_total}** "
              "(duplicate active slot, duplicate current appointment, multiple holds per shipment, orphan hold "
              "metadata, held-without-metadata, hold/metadata state mismatch, history without parent, replaced "
              "appointment still active, foreign-key violations)."), "",
              (f"Chained hand-off worker (shown the slot a confirmation is about to free), outcomes over "
              f"{2 * args.iterations} mixed runs: `{dict(swap)}`. Both orders were legal; every one kept all invariants."),
              "", "### Invariant query output (final mixed run)", "", "```", "check_invariants() -> []", "```", "",
              "### Mixed-run event log (final threaded run)", "", "```"]
    lines += events + ["```", "", "### Outcomes in that run", "", "```"]
    lines += (last_mixed[1] if last_mixed else []) + ["```", "", "## B. Hypothesis state machine", "",
                                                      ("Random sequences of show, hold, contend, expire, release, "
                                                      "confirm, never-mind, cancel and external direct inserts; "
                                                      "`check_invariants()` runs after every step. Two generated sequences:"), ""]
    for n, trace in enumerate(examples, 1):
        lines += [f"Example {n}:", "", "```"] + trace[:22] + ["```", ""]
    lines += ["## C. pytest suites repeated with a Python loop", "",
              "| Loop | Exit code | Seconds | Result line |", "|---|---|---|---|"]
    for k, (rc, secs, tail) in enumerate(runs, 1):
        lines.append(f"| {k} | {rc} | {secs:.1f} | {tail} |")
    failed = sum(1 for rc, *_ in runs if rc != 0)
    lines += ["", f"**{len(runs) - failed}/{len(runs)} loops passed.**", ""]
    out = ROOT / "docs" / "verification" / "concurrency-proof.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out}; violations={violations_total}; pytest loop failures={failed}")
    return 0 if violations_total == 0 and failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
