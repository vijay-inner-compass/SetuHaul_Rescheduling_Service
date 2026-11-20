"""Run the Phase 11 scale profile and write docs/verification/scale-test.md.

Profile (stub model, generated 360-load fixture, one uvicorn worker, in-process fake Redis, fixed clock):
  A  spike: 35 driver messages over 35 s (the brief's 35 over 30 minutes, compressed by ~51x)
  B  burst: 10 drivers, all shown the same best slot, all asking to hold it at the same instant
  C  the same spike driven by Locust (repeatability)
  D  in-process timings against a copy of the fixture: capacity search, hold and confirm write transactions
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import httpx
from tests.helpers import at

from setuhaul.booking import ConfirmCommand, acquire_hold, confirm_reschedule
from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from setuhaul.eta import load_eta_records
from setuhaul.feasibility import search_for_shipment
from setuhaul.integrity import check_invariants
from setuhaul.scale_fixture import generated_driver_phone, make_scale_fixture
from setuhaul.schemas import BaselineType, EtaEvidence

SECRET = "load-secret"
NOW = "2026-08-04T09:35:00+05:30"


def pct(values: list[float], p: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, max(0, round(p / 100 * len(s)) - 1))] if s else 0.0


def stats(values: list[float]) -> dict[str, float]:
    return {"n": len(values), "p50_ms": round(pct(values, 50), 1), "p95_ms": round(pct(values, 95), 1),
            "p99_ms": round(pct(values, 99), 1), "max_ms": round(max(values, default=0.0), 1)}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def signed(client: httpx.Client, base: str, i: int, mid: str, text: str) -> tuple[float, int, dict[str, Any]]:
    body = json.dumps({"from": generated_driver_phone(i), "message_id": mid, "text": text}).encode()
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    t0 = time.perf_counter()
    r = client.post(f"{base}/v1/driver/messages", content=body, headers={"x-signature": sig}, timeout=30)
    ms = (time.perf_counter() - t0) * 1000
    try:
        data = r.json()
    except ValueError:
        data = {}
    return ms, r.status_code, data


def inprocess(fixture: Path) -> dict[str, Any]:
    """D: capacity search and write-transaction timings on a private copy of the fixture."""
    work = Path(tempfile.mkdtemp()) / "inproc.sqlite3"
    shutil.copy(fixture, work)
    conn = open_connection(path=work)
    clock = FakeClock(at("09:35"))
    rng = random.Random(3)
    ids = [r[0] for r in conn.execute("select shipment_id from shipments where shipment_id like 'GEN-%' order by 1")]
    search_ms = []
    for sid in rng.sample(ids, 200):
        planned, _ = load_eta_records(conn, sid)
        assert planned is not None
        eta = EtaEvidence(eta=planned.eta, source="DRIVER_DECLARED", confidence="HIGH", baseline_type=BaselineType.PLANNED_ETA)
        t0 = time.perf_counter()
        search_for_shipment(conn, sid, eta, clock)
        search_ms.append((time.perf_counter() - t0) * 1000)
    # write transactions: hold then confirm, sequentially, on distinct burst-group shipments' options
    from setuhaul.allocation import rank_options
    from setuhaul.capacity import build_facility_state
    from setuhaul.feasibility import build_context, search_feasible_slots
    from setuhaul.option_history import record_option_set

    hold_ms, confirm_ms = [], []
    for i in range(13, 73):  # scheduled loads without a hold; each gets its own option set
        sid = f"GEN-SHP{i:04d}"
        thread = f"GEN-THR{i:04d}"
        planned, _ = load_eta_records(conn, sid)
        assert planned is not None
        eta = EtaEvidence(eta=planned.eta, source="DRIVER_DECLARED", confidence="HIGH", baseline_type=BaselineType.PLANNED_ETA)
        fctx = build_context(conn, sid, eta, clock.now())
        state = build_facility_state(conn, fctx.facility_id, clock.now())
        ranked = rank_options(search_feasible_slots(fctx, state).feasible, 3)
        if not ranked:
            continue
        opt = record_option_set(conn, fctx, thread, ranked, state, clock)
        driver = f"GEN-DRV{i:04d}"
        t0 = time.perf_counter()
        held = acquire_hold(conn, driver, thread, sid, opt.option_set_id, 1, clock)
        hold_ms.append((time.perf_counter() - t0) * 1000)
        if held.status == "HELD":
            t0 = time.perf_counter()
            confirm_reschedule(conn, ConfirmCommand(driver_id=driver, thread_id=thread, message_id=f"C{i}", shipment_id=sid,
                                                    hold_appointment_id=str(held.appointment_id)), clock)
            confirm_ms.append((time.perf_counter() - t0) * 1000)
    violations = check_invariants(conn)
    conn.close()
    return {"capacity_search": stats(search_ms), "hold_txn": stats(hold_ms), "confirm_txn": stats(confirm_ms),
            "invariant_violations": len(violations)}


def saturation(fixture: Path, writers_list: tuple[int, ...] = (1, 4, 8, 16), seconds: float = 4.0) -> list[dict[str, Any]]:
    """E: how hard can several writer connections push SQLite? hold -> release loops, one connection per writer."""
    from setuhaul.allocation import rank_options
    from setuhaul.booking import OutcomeClass, release_hold
    from setuhaul.capacity import build_facility_state
    from setuhaul.feasibility import build_context, search_feasible_slots
    from setuhaul.option_history import record_option_set

    out = []
    for writers in writers_list:
        work = Path(tempfile.mkdtemp()) / "sat.sqlite3"
        shutil.copy(fixture, work)
        setup = open_connection(path=work)
        clock = FakeClock(at("09:35"))
        jobs = []
        for k in range(writers):
            i = 100 + k
            sid, thread, driver = f"GEN-SHP{i:04d}", f"GEN-THR{i:04d}", f"GEN-DRV{i:04d}"
            planned, _ = load_eta_records(setup, sid)
            assert planned is not None
            eta = EtaEvidence(eta=planned.eta, source="DRIVER_DECLARED", confidence="HIGH", baseline_type=BaselineType.PLANNED_ETA)
            fctx = build_context(setup, sid, eta, clock.now())
            state = build_facility_state(setup, fctx.facility_id, clock.now())
            ranked = rank_options(search_feasible_slots(fctx, state).feasible, 3)
            jobs.append((driver, thread, sid, record_option_set(setup, fctx, thread, ranked, state, clock).option_set_id, bool(ranked)))
        setup.close()
        lat: list[float] = []
        classes: dict[str, int] = {}
        lock = threading.Lock()
        stop_at = time.perf_counter() + seconds
        start = threading.Barrier(writers)

        def loop(job: tuple[str, str, str, str, bool]) -> None:
            driver, thread, sid, opt, usable = job
            conn = open_connection(path=work)
            start.wait()
            while usable and time.perf_counter() < stop_at:
                t0 = time.perf_counter()
                r = acquire_hold(conn, driver, thread, sid, opt, 1, clock)
                ms = (time.perf_counter() - t0) * 1000
                with lock:
                    lat.append(ms)
                    classes[r.outcome.value] = classes.get(r.outcome.value, 0) + 1
                if r.status == "HELD":
                    release_hold(conn, driver, thread, str(r.appointment_id), "DRIVER_CANCELLED", clock)
            conn.close()

        ts = [threading.Thread(target=loop, args=(j,)) for j in jobs]
        [t.start() for t in ts]
        [t.join() for t in ts]
        total = sum(classes.values())
        chk = open_connection(path=work, readonly=True)
        viol = len(check_invariants(chk))
        chk.close()
        out.append({"writers": writers, "hold_attempts": total, "attempts_per_s": round(total / seconds, 1), **{k: v for k, v in stats(lat).items() if k != "n"},
                    "outcomes": classes, "lock_timeout_rate_pct": round(100 * classes.get(OutcomeClass.LOCK_TIMEOUT.value, 0) / max(1, total), 2),
                    "invariant_violations": viol})
    return out


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="setuhaul-scale-"))
    fixture = work / "scale.sqlite3"
    summary = make_scale_fixture(fixture)
    print("fixture:", {k: v for k, v in summary.items() if k not in ("path", "burst")})
    print("D: in-process timings ...", flush=True)
    inproc = inprocess(fixture)
    print("E: write saturation ...", flush=True)
    sat = saturation(fixture)

    server_db = work / "server.sqlite3"
    shutil.copy(fixture, server_db)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "SETUHAUL_DB_PATH": str(server_db), "SETUHAUL_REDIS_URL": "fakeredis://", "SETUHAUL_USE_STUB_MODEL": "1",
           "SETUHAUL_WEBHOOK_SECRET": SECRET, "SETUHAUL_COORDINATOR_TOKENS": "{}", "SETUHAUL_RUN_MAINTENANCE": "1",
           "SETUHAUL_FAKE_NOW": NOW, "SETUHAUL_FALLBACK_ESCALATIONS": str(work / "fallback.jsonl"), "PYTHONPATH": str(ROOT / "src")}
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "--factory", "setuhaul.api:create_app_from_env", "--port", str(port),
                               "--log-level", "warning"], env=env, cwd=ROOT)
    results: dict[str, Any] = {"fixture": {k: v for k, v in summary.items() if k not in ("path", "burst")}, "in_process": inproc, "saturation": sat,
                               "workers": 1, "redis": "in-process fakeredis", "compression": round(1800 / 35, 1)}
    try:
        with httpx.Client() as probe:
            for _ in range(100):
                try:
                    if probe.get(f"{base}/ready", timeout=2).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.3)
            else:
                raise RuntimeError("server did not become ready")

        # A: spike, 35 messages over 35 s
        print("A: spike ...", flush=True)
        rng = random.Random(5)
        offsets = sorted(rng.uniform(0, 34) for _ in range(35))
        a_lat: list[float] = []
        a_codes: list[int] = []
        a_status: list[str] = []
        lock = threading.Lock()
        t_start = time.perf_counter()

        def send(k: int, off: float) -> None:
            time.sleep(max(0, off - (time.perf_counter() - t_start)))
            with httpx.Client() as c:
                ms, code, data = signed(c, base, 20 + k, f"SPIKE-{k}", f"my truck is {rng.choice([30, 45, 60, 90])} minutes late")
            with lock:
                a_lat.append(ms)
                a_codes.append(code)
                a_status.append(str(data.get("status")))

        threads = [threading.Thread(target=send, args=(k, o)) for k, o in enumerate(offsets)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        results["spike"] = {**stats(a_lat), "http_codes": {str(c): a_codes.count(c) for c in sorted(set(a_codes))},
                            "reply_status": {s: a_status.count(s) for s in sorted(set(a_status))}}

        # B: burst, 10 drivers, one slot
        print("B: burst ...", flush=True)
        burst = list(range(1, 11))
        with httpx.Client() as c:
            for i in burst:
                signed(c, base, i, f"B-SHOW-{i}", "my truck is 60 minutes late")
        barrier = threading.Barrier(len(burst))
        b_out: list[tuple[float, int, dict[str, Any]]] = []

        def hold(i: int) -> None:
            with httpx.Client() as c:
                barrier.wait()
                r = signed(c, base, i, f"B-HOLD-{i}", "I will take the first one")
            with lock:
                b_out.append(r)

        ts = [threading.Thread(target=hold, args=(i,)) for i in burst]
        [t.start() for t in ts]
        [t.join() for t in ts]
        winners = [d for _, _, d in b_out if "holding" in str(d.get("reply", "")).lower()]
        results["burst"] = {**stats([m for m, _, _ in b_out]), "drivers": len(burst), "winners": len(winners),
                            "http_codes": {str(c): sum(1 for _, x, _ in b_out if x == c) for c in sorted({x for _, x, _ in b_out})},
                            "reply_status": {s: sum(1 for _, _, d in b_out if str(d.get("status")) == s) for s in sorted({str(d.get('status')) for _, _, d in b_out})}}

        # C: the same spike through Locust
        print("C: locust ...", flush=True)
        csvbase = work / "locust"
        subprocess.run([sys.executable, "-m", "locust", "-f", str(ROOT / "load" / "locustfile.py"), "--headless", "-u", "1", "-r", "1",
                        "-t", "35s", "--host", base, "--csv", str(csvbase), "--only-summary"],
                       env={**os.environ, "SETUHAUL_WEBHOOK_SECRET": SECRET}, cwd=ROOT, capture_output=True, text=True, check=False)
        try:
            with open(f"{csvbase}_stats.csv", encoding="utf-8") as fh:
                row = next(r for r in csv.DictReader(fh) if r["Name"] == "POST /v1/driver/messages")
            results["locust"] = {"requests": int(row["Request Count"]), "failures": int(row["Failure Count"]),
                                 "p50_ms": float(row["50%"]), "p95_ms": float(row["95%"]), "p99_ms": float(row["99%"])}
        except (OSError, StopIteration, KeyError):
            results["locust"] = {"error": "locust did not produce a stats file on this machine"}
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()

    conn = open_connection(path=server_db, readonly=True)
    violations = check_invariants(conn)
    held = conn.execute("select slot_id, count(*) from appointments where appointment_status='HELD' group by slot_id having count(*) > 1").fetchall()
    rows = conn.execute("select reply_json from inbound_messages where status='DONE'").fetchall()
    unavailable = sum(1 for (j,) in rows if json.loads(j).get("status") == "TEMPORARILY_UNAVAILABLE")
    results["after"] = {"invariant_violations": len(violations), "slots_with_more_than_one_hold": len(held), "messages_handled": len(rows),
                        "temporarily_unavailable": unavailable, "lock_or_system_error_rate_pct": round(100 * unavailable / max(1, len(rows)), 2),
                        "conflicting_allocations": len(violations) + len(held)}
    conn.close()
    (ROOT / "docs" / "verification" / "scale-results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))
    return 0 if results["after"]["conflicting_allocations"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
