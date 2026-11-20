"""Replay every trace in tests/traces/ through the deterministic layers (never a live model) and score it."""

from __future__ import annotations

import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import fakeredis
from tests.helpers import seed_everything_known

from setuhaul.db import make_working_db, open_connection
from setuhaul.evaluation import Trace, classify_failure, replay_trace


def replay_file(path: Path):  # type: ignore[no-untyped-def]
    trace = Trace.model_validate_json(path.read_text(encoding="utf-8"))
    d = Path(tempfile.mkdtemp())
    conn = open_connection(path=make_working_db(d / "w.sqlite3"))
    seed_everything_known(conn)
    try:
        return trace, replay_trace(trace, conn, fakeredis.FakeRedis(decode_responses=True), d / "fallback.jsonl")
    finally:
        conn.close()


def main() -> int:
    files = sorted((ROOT / "tests" / "traces").glob("*.json"))
    if not files:
        print("no traces found")
        return 1
    totals: Counter[str] = Counter()
    failed = 0
    for f in files:
        trace, res = replay_file(f)
        ok_n = sum(x.ok for x in res.findings)
        totals["checks"] += len(res.findings)
        totals["passed"] += ok_n
        tax = Counter(c for t in trace.turns for c in classify_failure(t))
        print(f"{f.name:<42} source={trace.source:<8} turns={len(trace.turns)} checks={ok_n}/{len(res.findings)} "
              f"mismatches={len(res.mismatches)} violations={len(res.violations)} taxonomy={dict(tax) or '-'}")
        for m in res.mismatches:
            print("   MISMATCH", m)
        for x in res.findings:
            if not x.ok:
                print("   FAIL", x)
        failed += 0 if res.ok else 1
    print(f"\n{len(files) - failed}/{len(files)} traces replayed clean; {totals['passed']}/{totals['checks']} evaluator checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
