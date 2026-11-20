"""Replay every trace in tests/traces/ through the deterministic layers (never a live model) and score it."""

# Lazy annotations.
from __future__ import annotations

# sys.path setup.
import sys

# A scratch folder for each replay's database.
import tempfile

# Counts failure classes.
from collections import Counter

# Path arithmetic.
from pathlib import Path

# The repository root.
ROOT = Path(__file__).resolve().parents[1]
# Make tests helpers importable.
sys.path.insert(0, str(ROOT))
# Make the service importable.
sys.path.insert(0, str(ROOT / "src"))

# An in-process fake Redis for replay.
import fakeredis

# Seeds the same evidence the traces were recorded against.
from tests.helpers import seed_everything_known

# Working database creation.
from setuhaul.db import make_working_db, open_connection

# Trace model, failure taxonomy and replay.
from setuhaul.evaluation import Trace, classify_failure, replay_trace


# Replay one trace file in a fresh database. Returns the trace and its result.
def replay_file(path: Path):  # type: ignore[no-untyped-def]
    # Load and validate the trace.
    trace = Trace.model_validate_json(path.read_text(encoding="utf-8"))
    # A scratch folder.
    d = Path(tempfile.mkdtemp())
    # A fresh copy of the seed database.
    conn = open_connection(path=make_working_db(d / "w.sqlite3"))
    # Add the same labour and consignee evidence the recording used.
    seed_everything_known(conn)
    try:
        # Replay through the real deterministic layers; no live model is called.
        return trace, replay_trace(trace, conn, fakeredis.FakeRedis(decode_responses=True), d / "fallback.jsonl")
    finally:
        # Always close the connection.
        conn.close()


# Replay every trace and print a one-line summary for each.
def main() -> int:
    # All trace files.
    files = sorted((ROOT / "tests" / "traces").glob("*.json"))
    # No traces: that is a failure, not a pass.
    if not files:
        print("no traces found")
        return 1
    # Running totals of evaluator checks.
    totals: Counter[str] = Counter()
    # How many traces failed.
    failed = 0
    # Each trace.
    for f in files:
        # Replay it.
        trace, res = replay_file(f)
        # Evaluator checks that passed.
        ok_n = sum(x.ok for x in res.findings)
        totals["checks"] += len(res.findings)
        totals["passed"] += ok_n
        # Failure classes seen in this trace.
        tax = Counter(c for t in trace.turns for c in classify_failure(t))
        # One summary line: source, turns, checks, mismatches, violations, taxonomy.
        print(f"{f.name:<42} source={trace.source:<8} turns={len(trace.turns)} checks={ok_n}/{len(res.findings)} "
              f"mismatches={len(res.mismatches)} violations={len(res.violations)} taxonomy={dict(tax) or '-'}")
        # Print any difference from the recorded behaviour.
        for m in res.mismatches:
            print("   MISMATCH", m)
        # Print any failed evaluator check.
        for x in res.findings:
            if not x.ok:
                print("   FAIL", x)
        # Count a trace as failed unless replay was fully clean.
        failed += 0 if res.ok else 1
    # Overall summary.
    print(f"\n{len(files) - failed}/{len(files)} traces replayed clean; {totals['passed']}/{totals['checks']} evaluator checks passed")
    # Non-zero exit if any trace failed.
    return 1 if failed else 0


# Run main when executed as a script.
if __name__ == "__main__":
    raise SystemExit(main())
