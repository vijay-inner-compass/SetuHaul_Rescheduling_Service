"""Run the Phase 11 scale profile and write docs/verification/scale-test.md.

Profile (stub model, generated 360-load fixture, one uvicorn worker, in-process fake Redis, fixed clock):
  A  spike: 35 driver messages over 35 s (the brief's 35 over 30 minutes, compressed by ~51x)
  B  burst: 10 drivers, all shown the same best slot, all asking to hold it at the same instant
  C  the same spike driven by Locust (repeatability)
  D  in-process timings against a copy of the fixture: capacity search, hold and confirm write transactions
"""

# Lazy annotations.
from __future__ import annotations

# Reads Locust's results file.
import csv

# SHA-256 for the webhook signature.
import hashlib

# Signs each request body as the real channel would.
import hmac

# Request and report encoding.
import json

# Environment variables for the server process.
import os

# Seeded random numbers for repeatable load.
import random

# Copies the fixture database.
import shutil

# Finds a free TCP port.
import socket

# Starts the server and Locust.
import subprocess

# sys.path and sys.executable.
import sys

# Scratch folders.
import tempfile

# Concurrent senders.
import threading

# Timing.
import time

# Path arithmetic.
from pathlib import Path

# Typing helper.
from typing import Any

# The repository root.
ROOT = Path(__file__).resolve().parents[1]
# Make tests helpers importable.
sys.path.insert(0, str(ROOT))
# Make the service importable.
sys.path.insert(0, str(ROOT / "src"))

# HTTP client used to call the running server.
import httpx

# Time helper.
from tests.helpers import at

# Hold and confirm operations, timed in-process.
from setuhaul.booking import ConfirmCommand, acquire_hold, confirm_reschedule

# Fixed clock.
from setuhaul.clock import FakeClock

# Connections.
from setuhaul.db import open_connection

# ETA records.
from setuhaul.eta import load_eta_records

# Slot search.
from setuhaul.feasibility import search_for_shipment

# Invariant checks after the run.
from setuhaul.integrity import check_invariants

# The generated 360-load database.
from setuhaul.scale_fixture import generated_driver_phone, make_scale_fixture

# ETA evidence types.
from setuhaul.schemas import BaselineType, EtaEvidence

# The webhook secret shared by the load generator and the test server (a test value).
SECRET = "load-secret"
# The fixed 'now' of the server (the snapshot is dated 4 Aug 2026).
NOW = "2026-08-04T09:35:00+05:30"


# The p-th percentile of a list of timings.
def pct(values: list[float], p: float) -> float:
    # Sort ascending.
    s = sorted(values)
    return s[min(len(s) - 1, max(0, round(p / 100 * len(s)) - 1))] if s else 0.0


# Summary: count, p50, p95, p99 and max, in milliseconds.
def stats(values: list[float]) -> dict[str, float]:
    return {"n": len(values), "p50_ms": round(pct(values, 50), 1), "p95_ms": round(pct(values, 95), 1),
            "p99_ms": round(pct(values, 99), 1), "max_ms": round(max(values, default=0.0), 1)}


# Ask the operating system for an unused port.
def free_port() -> int:
    # A throwaway socket.
    with socket.socket() as s:
        # Port 0 means 'pick any free port'.
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# Send one correctly signed webhook message and time it.
def signed(client: httpx.Client, base: str, i: int, mid: str, text: str) -> tuple[float, int, dict[str, Any]]:
    # The request body.
    body = json.dumps({"from": generated_driver_phone(i), "message_id": mid, "text": text}).encode()
    # The HMAC-SHA256 signature over the raw body.
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    # Start timing.
    t0 = time.perf_counter()
    # Send the message.
    r = client.post(f"{base}/v1/driver/messages", content=body, headers={"x-signature": sig}, timeout=30)
    # Elapsed milliseconds.
    ms = (time.perf_counter() - t0) * 1000
    # Everything below must clean up the server even if it fails.
    try:
        # The decoded response.
        data = r.json()
    # A non-JSON reply gives empty data.
    except ValueError:
        data = {}
    return ms, r.status_code, data


def inprocess(fixture: Path) -> dict[str, Any]:
    """D: capacity search and write-transaction timings on a private copy of the fixture."""
    # A private copy of the fixture.
    work = Path(tempfile.mkdtemp()) / "inproc.sqlite3"
    # Copy it.
    shutil.copy(fixture, work)
    # Open the copy.
    conn = open_connection(path=work)
    # Fixed time.
    clock = FakeClock(at("09:35"))
    # Seeded random choice.
    rng = random.Random(3)
    # All generated shipments.
    ids = [r[0] for r in conn.execute("select shipment_id from shipments where shipment_id like 'GEN-%' order by 1")]
    # Timings of slot searches.
    search_ms = []
    # Search for 200 random loads.
    for sid in rng.sample(ids, 200):
        # The load's planned ETA.
        planned, _ = load_eta_records(conn, sid)
        # Generated loads always have one.
        assert planned is not None
        # Treat the planned ETA as the arrival time.
        eta = EtaEvidence(eta=planned.eta, source="DRIVER_DECLARED", confidence="HIGH", baseline_type=BaselineType.PLANNED_ETA)
        t0 = time.perf_counter()
        # Run the capacity search.
        search_for_shipment(conn, sid, eta, clock)
        # Record the time.
        search_ms.append((time.perf_counter() - t0) * 1000)
    # write transactions: hold then confirm, sequentially, on distinct burst-group shipments' options
    from setuhaul.allocation import rank_options
    from setuhaul.capacity import build_facility_state
    from setuhaul.feasibility import build_context, search_feasible_slots
    from setuhaul.option_history import record_option_set

    # Timings of hold and confirm transactions.
    hold_ms, confirm_ms = [], []
    # Sixty scheduled loads without a hold.
    for i in range(13, 73):  # scheduled loads without a hold; each gets its own option set
        # Their shipment id.
        sid = f"GEN-SHP{i:04d}"
        # Their conversation thread.
        thread = f"GEN-THR{i:04d}"
        planned, _ = load_eta_records(conn, sid)
        assert planned is not None
        eta = EtaEvidence(eta=planned.eta, source="DRIVER_DECLARED", confidence="HIGH", baseline_type=BaselineType.PLANNED_ETA)
        # The feasibility context.
        fctx = build_context(conn, sid, eta, clock.now())
        # A fresh facility snapshot.
        state = build_facility_state(conn, fctx.facility_id, clock.now())
        # The best three feasible slots.
        ranked = rank_options(search_feasible_slots(fctx, state).feasible, 3)
        # Nothing feasible: skip this load.
        if not ranked:
            continue
        # Store the menu.
        opt = record_option_set(conn, fctx, thread, ranked, state, clock)
        # The driver id.
        driver = f"GEN-DRV{i:04d}"
        t0 = time.perf_counter()
        # Time the hold transaction.
        held = acquire_hold(conn, driver, thread, sid, opt.option_set_id, 1, clock)
        # Record it.
        hold_ms.append((time.perf_counter() - t0) * 1000)
        # Only confirm a hold that succeeded.
        if held.status == "HELD":
            t0 = time.perf_counter()
            # Time the confirmation transaction.
            confirm_reschedule(conn, ConfirmCommand(driver_id=driver, thread_id=thread, message_id=f"C{i}", shipment_id=sid,
                                                    hold_appointment_id=str(held.appointment_id)), clock)
            # Record it.
            confirm_ms.append((time.perf_counter() - t0) * 1000)
    # Verify the database is consistent after all those writes.
    violations = check_invariants(conn)
    # Close the connection.
    conn.close()
    # The in-process results.
    return {"capacity_search": stats(search_ms), "hold_txn": stats(hold_ms), "confirm_txn": stats(confirm_ms),
            "invariant_violations": len(violations)}


def saturation(fixture: Path, writers_list: tuple[int, ...] = (1, 4, 8, 16), seconds: float = 4.0) -> list[dict[str, Any]]:
    """E: how hard can several writer connections push SQLite? hold -> release loops, one connection per writer."""
    from setuhaul.allocation import rank_options
    from setuhaul.booking import OutcomeClass, release_hold
    from setuhaul.capacity import build_facility_state
    from setuhaul.feasibility import build_context, search_feasible_slots
    from setuhaul.option_history import record_option_set

    # One result per writer count.
    out = []
    # Try 1, 4, 8 and 16 concurrent writers.
    for writers in writers_list:
        # A fresh copy for each level.
        work = Path(tempfile.mkdtemp()) / "sat.sqlite3"
        shutil.copy(fixture, work)
        # A setup connection.
        setup = open_connection(path=work)
        clock = FakeClock(at("09:35"))
        # One job per writer.
        jobs = []
        # Prepare each writer's load and menu.
        for k in range(writers):
            # Use loads 100 and up.
            i = 100 + k
            sid, thread, driver = f"GEN-SHP{i:04d}", f"GEN-THR{i:04d}", f"GEN-DRV{i:04d}"
            planned, _ = load_eta_records(setup, sid)
            assert planned is not None
            eta = EtaEvidence(eta=planned.eta, source="DRIVER_DECLARED", confidence="HIGH", baseline_type=BaselineType.PLANNED_ETA)
            fctx = build_context(setup, sid, eta, clock.now())
            state = build_facility_state(setup, fctx.facility_id, clock.now())
            ranked = rank_options(search_feasible_slots(fctx, state).feasible, 3)
            jobs.append((driver, thread, sid, record_option_set(setup, fctx, thread, ranked, state, clock).option_set_id, bool(ranked)))
        # Setup done.
        setup.close()
        # Hold latencies.
        lat: list[float] = []
        # Outcome class counts.
        classes: dict[str, int] = {}
        # Protects the shared lists.
        lock = threading.Lock()
        # When to stop (4 seconds).
        stop_at = time.perf_counter() + seconds
        # All writers start together.
        start = threading.Barrier(writers)

        # Each writer: hold, then release, repeatedly.
        def loop(job: tuple[str, str, str, str, bool], work: Path = work, start: threading.Barrier = start, stop_at: float = stop_at,
                 clock: FakeClock = clock, lock: threading.Lock = lock, lat: list[float] = lat, classes: dict[str, int] = classes) -> None:
            # Unpack the job.
            driver, thread, sid, opt, usable = job
            # Each writer has its own connection.
            conn = open_connection(path=work)
            # Wait for the others.
            start.wait()
            # Keep going until time is up.
            while usable and time.perf_counter() < stop_at:
                t0 = time.perf_counter()
                # Try to hold.
                r = acquire_hold(conn, driver, thread, sid, opt, 1, clock)
                ms = (time.perf_counter() - t0) * 1000
                # Record under the lock.
                with lock:
                    lat.append(ms)
                    classes[r.outcome.value] = classes.get(r.outcome.value, 0) + 1
                # Release it so the next iteration can hold again.
                if r.status == "HELD":
                    release_hold(conn, driver, thread, str(r.appointment_id), "DRIVER_CANCELLED", clock)
            conn.close()

        # Start the writers.
        ts = [threading.Thread(target=loop, args=(j,)) for j in jobs]
        # Start them all.
        [t.start() for t in ts]
        # Wait for them to finish.
        [t.join() for t in ts]
        # Total attempts.
        total = sum(classes.values())
        # Read-only check afterwards.
        chk = open_connection(path=work, readonly=True)
        # Invariant violations (must be 0).
        viol = len(check_invariants(chk))
        chk.close()
        # Throughput, latency percentiles, outcome mix and the lock-timeout rate.
        out.append({"writers": writers, "hold_attempts": total, "attempts_per_s": round(total / seconds, 1), **{k: v for k, v in stats(lat).items() if k != "n"},
                    "outcomes": classes, "lock_timeout_rate_pct": round(100 * classes.get(OutcomeClass.LOCK_TIMEOUT.value, 0) / max(1, total), 2),
                    "invariant_violations": viol})
    # All levels.
    return out


# Run the full profile and write the results.
def main() -> int:
    # A scratch folder.
    work = Path(tempfile.mkdtemp(prefix="setuhaul-scale-"))
    # The fixture file.
    fixture = work / "scale.sqlite3"
    # Generate about 360 loads.
    summary = make_scale_fixture(fixture)
    # Show what was generated.
    print("fixture:", {k: v for k, v in summary.items() if k not in ("path", "burst")})
    # Step D.
    print("D: in-process timings ...", flush=True)
    # Time search, hold and confirm in-process.
    inproc = inprocess(fixture)
    # Step E.
    print("E: write saturation ...", flush=True)
    # Find how hard SQLite can be pushed.
    sat = saturation(fixture)

    # A copy of the fixture for the server.
    server_db = work / "server.sqlite3"
    # Copy it.
    shutil.copy(fixture, server_db)
    # A free port.
    port = free_port()
    # The server's address.
    base = f"http://127.0.0.1:{port}"
    # The server's configuration: stub model, fake Redis, fixed clock, background maintenance on.
    env = {**os.environ, "SETUHAUL_DB_PATH": str(server_db), "SETUHAUL_REDIS_URL": "fakeredis://", "SETUHAUL_USE_STUB_MODEL": "1",
           "SETUHAUL_WEBHOOK_SECRET": SECRET, "SETUHAUL_COORDINATOR_TOKENS": "{}", "SETUHAUL_RUN_MAINTENANCE": "1",
           "SETUHAUL_FAKE_NOW": NOW, "SETUHAUL_FALLBACK_ESCALATIONS": str(work / "fallback.jsonl"), "PYTHONPATH": str(ROOT / "src")}
    # Start the real server as a separate process (one worker).
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "--factory", "setuhaul.api:create_app_from_env", "--port", str(port),
                               "--log-level", "warning"], env=env, cwd=ROOT)
    # The results so far.
    results: dict[str, Any] = {"fixture": {k: v for k, v in summary.items() if k not in ("path", "burst")}, "in_process": inproc, "saturation": sat,
                               "workers": 1, "redis": "in-process fakeredis", "compression": round(1800 / 35, 1)}
    try:
        # Wait for the server to be ready.
        with httpx.Client() as probe:
            # Poll up to about 30 seconds.
            for _ in range(100):
                try:
                    # Ready returns 200.
                    if probe.get(f"{base}/ready", timeout=2).status_code == 200:
                        break
                # Not up yet: wait and retry.
                except httpx.HTTPError:
                    time.sleep(0.3)
            # Never became ready.
            else:
                raise RuntimeError("server did not become ready")

        # A: spike, 35 messages over 35 s
        # Step A: 35 messages over 35 seconds.
        print("A: spike ...", flush=True)
        # Seeded so the spike is repeatable.
        rng = random.Random(5)
        # Random send times between 0 and 34 seconds.
        offsets = sorted(rng.uniform(0, 34) for _ in range(35))
        # Latencies.
        a_lat: list[float] = []
        # HTTP status codes.
        a_codes: list[int] = []
        # Reply statuses.
        a_status: list[str] = []
        # Protects the lists.
        lock = threading.Lock()
        # Spike start.
        t_start = time.perf_counter()

        # Send one message at its scheduled time.
        def send(k: int, off: float) -> None:
            # Wait until this message's turn.
            time.sleep(max(0, off - (time.perf_counter() - t_start)))
            with httpx.Client() as c:
                # Send it, signed.
                ms, code, data = signed(c, base, 20 + k, f"SPIKE-{k}", f"my truck is {rng.choice([30, 45, 60, 90])} minutes late")
            with lock:
                a_lat.append(ms)
                a_codes.append(code)
                a_status.append(str(data.get("status")))

        # One thread per message.
        threads = [threading.Thread(target=send, args=(k, o)) for k, o in enumerate(offsets)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        # Latency percentiles, status codes and reply statuses.
        results["spike"] = {**stats(a_lat), "http_codes": {str(c): a_codes.count(c) for c in sorted(set(a_codes))},
                            "reply_status": {s: a_status.count(s) for s in sorted(set(a_status))}}

        # B: burst, 10 drivers, one slot
        # Step B: ten drivers all try to hold the same slot at the same instant.
        print("B: burst ...", flush=True)
        # The ten burst drivers.
        burst = list(range(1, 11))
        with httpx.Client() as c:
            # First show each driver the same options.
            for i in burst:
                signed(c, base, i, f"B-SHOW-{i}", "my truck is 60 minutes late")
        # Release all ten at once.
        barrier = threading.Barrier(len(burst))
        # Their results.
        b_out: list[tuple[float, int, dict[str, Any]]] = []

        # One driver's hold attempt.
        def hold(i: int) -> None:
            with httpx.Client() as c:
                # Wait for the others.
                barrier.wait()
                # 'I will take the first one'.
                r = signed(c, base, i, f"B-HOLD-{i}", "I will take the first one")
            with lock:
                b_out.append(r)

        ts = [threading.Thread(target=hold, args=(i,)) for i in burst]
        [t.start() for t in ts]
        [t.join() for t in ts]
        # Drivers whose reply says a hold was placed: exactly one is expected.
        winners = [d for _, _, d in b_out if "holding" in str(d.get("reply", "")).lower()]
        # The burst outcome.
        results["burst"] = {**stats([m for m, _, _ in b_out]), "drivers": len(burst), "winners": len(winners),
                            "http_codes": {str(c): sum(1 for _, x, _ in b_out if x == c) for c in sorted({x for _, x, _ in b_out})},
                            "reply_status": {s: sum(1 for _, _, d in b_out if str(d.get("status")) == s) for s in sorted({str(d.get('status')) for _, _, d in b_out})}}

        # C: the same spike through Locust
        # Step C: the same spike driven by Locust, for repeatability.
        print("C: locust ...", flush=True)
        # Where Locust writes its CSV.
        csvbase = work / "locust"
        # Run Locust headless for 35 seconds.
        subprocess.run([sys.executable, "-m", "locust", "-f", str(ROOT / "load" / "locustfile.py"), "--headless", "-u", "1", "-r", "1",
                        "-t", "35s", "--host", base, "--csv", str(csvbase), "--only-summary"],
                       env={**os.environ, "SETUHAUL_WEBHOOK_SECRET": SECRET}, cwd=ROOT, capture_output=True, text=True, check=False)
        try:
            # Read the results.
            with open(f"{csvbase}_stats.csv", encoding="utf-8") as fh:
                # The row for the message endpoint.
                row = next(r for r in csv.DictReader(fh) if r["Name"] == "POST /v1/driver/messages")
            # Request count, failures and percentiles.
            results["locust"] = {"requests": int(row["Request Count"]), "failures": int(row["Failure Count"]),
                                 "p50_ms": float(row["50%"]), "p95_ms": float(row["95%"]), "p99_ms": float(row["99%"])}
        # No stats file: say so.
        except (OSError, StopIteration, KeyError):
            results["locust"] = {"error": "locust did not produce a stats file on this machine"}
    # Always stop the server.
    finally:
        # Ask it to stop.
        server.terminate()
        try:
            # Wait for it.
            server.wait(timeout=15)
        # It did not stop: kill it.
        except subprocess.TimeoutExpired:
            server.kill()

    # Check the server's database after the run (read-only).
    conn = open_connection(path=server_db, readonly=True)
    # Invariant violations.
    violations = check_invariants(conn)
    # Any slot with more than one hold (must be none).
    held = conn.execute("select slot_id, count(*) from appointments where appointment_status='HELD' group by slot_id having count(*) > 1").fetchall()
    # Every message the server handled.
    rows = conn.execute("select reply_json from inbound_messages where status='DONE'").fetchall()
    # How many were answered 'temporarily unavailable'.
    unavailable = sum(1 for (j,) in rows if json.loads(j).get("status") == "TEMPORARILY_UNAVAILABLE")
    # The after-run checks: violations, double holds, messages and the error rate.
    results["after"] = {"invariant_violations": len(violations), "slots_with_more_than_one_hold": len(held), "messages_handled": len(rows),
                        "temporarily_unavailable": unavailable, "lock_or_system_error_rate_pct": round(100 * unavailable / max(1, len(rows)), 2),
                        "conflicting_allocations": len(violations) + len(held)}
    conn.close()
    # Write the results file.
    (ROOT / "docs" / "verification" / "scale-results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    # Print them.
    print(json.dumps(results, indent=2))
    # Success only if there were no conflicting allocations.
    return 0 if results["after"]["conflicting_allocations"] == 0 else 1


# Run main when executed as a script.
if __name__ == "__main__":
    raise SystemExit(main())
