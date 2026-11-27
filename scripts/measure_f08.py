"""Repeatable F08 performance measurement across sample, 14d, 90d and 365d histories.

Always runs on throwaway temporary copies of data/setuhaul.sqlite3; never modifies the tracked seed.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import fakeredis
from fastapi.testclient import TestClient

from setuhaul.api import AppDeps, create_app
from setuhaul.auth import sessions, users
from setuhaul.auth.warehouse_routes import driver_chats_in_scope
from setuhaul.capacity import build_facility_state
from setuhaul.clock import FakeClock
from setuhaul.config import load_settings
from setuhaul.db import IST, apply_migrations, immediate, open_connection
from setuhaul.feasibility import build_context, search_feasible_slots
from setuhaul.memory import ConversationMemoryStore
from setuhaul.schemas import BaselineType, EtaEvidence

AS_OF = datetime(2026, 8, 4, 9, 35, tzinfo=IST)
FACILITY_ID = "FAC-JAI-01"
SHIPMENT_ID = "SHP1006"


def _populate_history(conn: sqlite3.Connection, days: int) -> None:
    """Populate `days` days of hourly slots (past + 14 days ahead), labour windows, dock events, and chat messages."""
    docks = [r[0] for r in conn.execute("select dock_id from docks where facility_id=?", (FACILITY_ID,))]
    # Ensure 9 docks total (matching the review's 9-dock measurement) for non-sample horizons.
    for idx in range(len(docks) + 1, 10):
        did = f"DOCK-BENCH-{idx:02d}"
        conn.execute(
            "insert or ignore into docks (dock_id, facility_id, dock_code, dock_type, supports_refrigerated, "
            "max_vehicle_weight_kg, dock_status) values (?, ?, ?, 'STANDARD', 1, 25000, 'ACTIVE')",
            (did, FACILITY_ID, f"B{idx}"),
        )
        docks.append(did)

    start_day = (AS_OF - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    total_days = days + 14
    slot_rows: list[tuple[str, str, str, str, str, str, str]] = []
    labour_rows: list[tuple[str, str, str, int, int, str, str]] = []
    event_rows: list[tuple[str, str, str, str, str, str]] = []
    stamp = AS_OF.isoformat()

    for d_off in range(total_days):
        day = start_day + timedelta(days=d_off)
        labour_rows.append((
            FACILITY_ID, day.isoformat(), (day + timedelta(days=1)).isoformat(), 4, 1, "BENCH", stamp
        ))
        if d_off % 3 == 0:
            ev_s = day + timedelta(hours=2)
            ev_e = ev_s + timedelta(hours=1)
            event_rows.append((
                f"DEV-B-{d_off}", docks[d_off % len(docks)], "MAINTENANCE", ev_s.isoformat(), ev_e.isoformat(), "bench"
            ))
        for dock_id in docks:
            for hour in range(24):
                s = day + timedelta(hours=hour)
                e = s + timedelta(hours=1)
                slot_rows.append((
                    f"SB-{dock_id}-{d_off}-{hour:02d}", FACILITY_ID, dock_id,
                    s.isoformat(), e.isoformat(), "OPEN", stamp
                ))

    conn.executemany(
        "insert or ignore into appointment_slots (slot_id, facility_id, dock_id, slot_start_ts, slot_end_ts, slot_status, created_at) "
        "values (?,?,?,?,?,?,?)",
        slot_rows,
    )
    conn.executemany(
        "insert into labour_windows (facility_id, window_start, window_end, available_crews, equipment_available, source, recorded_at) "
        "values (?,?,?,?,?,?,?)",
        labour_rows,
    )
    conn.executemany(
        "insert or ignore into dock_status_events (dock_event_id, dock_id, event_type, event_start_ts, event_end_ts, reason) "
        "values (?,?,?,?,?,?)",
        event_rows,
    )

    # Chat history scaling with days (20 messages/day -> 7,300 messages at 365d).
    drv = conn.execute("select driver_id from shipments where shipment_id=?", (SHIPMENT_ID,)).fetchone()[0]
    conn.execute(
        "insert or ignore into chat_threads (thread_id, driver_id, shipment_id, opened_at, thread_status, thread_intent) "
        "values ('THR-BENCH', ?, ?, ?, 'OPEN', 'REPORT_DELAY')",
        (drv, SHIPMENT_ID, start_day.isoformat()),
    )
    msg_rows = []
    load_rows = []
    for i in range(days * 20):
        mid = f"MSGB-{i:06d}"
        ts = (start_day + timedelta(minutes=i * 70)).isoformat()
        msg_rows.append((mid, "THR-BENCH", "DRIVER" if i % 2 == 0 else "AGENT", f"bench message {i}", ts))
        load_rows.append((mid, SHIPMENT_ID, FACILITY_ID))
    conn.executemany(
        "insert or ignore into chat_messages (chat_message_id, thread_id, sender_type, message_text, message_ts) values (?,?,?,?,?)",
        msg_rows,
    )
    conn.executemany(
        "insert or ignore into chat_message_loads (chat_message_id, shipment_id, facility_id) values (?,?,?)",
        load_rows,
    )
    conn.commit()


def measure_horizon(label: str, days: int) -> dict[str, float | int | str]:
    tmp = Path(tempfile.mkdtemp(prefix=f"f08_{label}_"))
    try:
        db_path = tmp / "working.sqlite3"
        shutil.copy("data/setuhaul.sqlite3", db_path)
        conn = open_connection(db_path)
        apply_migrations(conn)
        if days > 0:
            _populate_history(conn, days)
        clock = FakeClock(AS_OF)
        admin = users.bootstrap_admin(conn, "bench@x.com", clock)
        assert admin is not None
        wh_user = users.invite(conn, actor=admin, email="wh@x.com", role="WAREHOUSE_AUTHORITY",
                               display_name="WH", facilities=(FACILITY_ID,), clock=clock)
        token = sessions.create(conn, wh_user, "GOOGLE", clock)
        conn.commit()

        slot_count = int(conn.execute("select count(*) from appointment_slots where facility_id=?", (FACILITY_ID,)).fetchone()[0])

        # 1. build_facility_state (unlocked) & write-lock hold time
        t0 = time.perf_counter()
        build_facility_state(conn, FACILITY_ID, AS_OF)
        fac_ms = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        with immediate(conn):
            build_facility_state(conn, FACILITY_ID, AS_OF)
        lock_ms = (time.perf_counter() - t0) * 1000

        # 2. Slot search (full facility load + evaluate + rank)
        eta = EtaEvidence(eta=AS_OF + timedelta(hours=2), source="DRIVER_DECLARED", confidence="MEDIUM", baseline_type=BaselineType.CURRENT_ETA)
        ctx = build_context(conn, SHIPMENT_ID, eta, AS_OF)
        t0 = time.perf_counter()
        st = build_facility_state(conn, FACILITY_ID, AS_OF)
        search_feasible_slots(ctx, st)
        search_ms = (time.perf_counter() - t0) * 1000

        # 3. _facility_chats (non-admin) & ConversationMemoryStore._history_turns
        t0 = time.perf_counter()
        driver_chats_in_scope(conn, wh_user, AS_OF)
        chats_ms = (time.perf_counter() - t0) * 1000

        mem = ConversationMemoryStore(conn, fakeredis.FakeRedis(decode_responses=True), clock, load_settings())
        t0 = time.perf_counter()
        mem._history_turns("THR-BENCH" if days > 0 else "THR001")
        mem_ms = (time.perf_counter() - t0) * 1000
        conn.close()

        # 4. /warehouse full & X-Live: 1
        deps = AppDeps(
            db_path=db_path,
            redis=fakeredis.FakeRedis(decode_responses=True),
            webhook_secret=b"s",
            coordinator_tokens={},
            fallback_path=tmp / "fb.jsonl",
            clock=clock,
        )
        client = TestClient(create_app(deps))
        client.cookies.set(sessions.COOKIE_NAME, token)
        t0 = time.perf_counter()
        r_full = client.get(f"/warehouse?facility={FACILITY_ID}")
        wh_full_ms = (time.perf_counter() - t0) * 1000
        assert r_full.status_code == 200
        wh_full_kib = len(r_full.content) / 1024

        t0 = time.perf_counter()
        r_live = client.get(f"/warehouse?facility={FACILITY_ID}", headers={"X-Live": "1"})
        wh_live_ms = (time.perf_counter() - t0) * 1000
        assert r_live.status_code == 200
        wh_live_kib = len(r_live.content) / 1024

        return {
            "horizon": label,
            "slots": slot_count,
            "fac_ms": round(fac_ms, 1),
            "lock_ms": round(lock_ms, 1),
            "search_ms": round(search_ms, 1),
            "wh_full_ms": round(wh_full_ms, 1),
            "wh_full_kib": round(wh_full_kib, 1),
            "wh_live_ms": round(wh_live_ms, 1),
            "wh_live_kib": round(wh_live_kib, 1),
            "chats_ms": round(chats_ms, 1),
            "mem_ms": round(mem_ms, 1),
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    print(f"{'horizon':<8} {'slots':>7} {'fac_ms':>8} {'lock_ms':>8} {'srch_ms':>8} "
          f"{'wh_ms':>8} {'wh_KiB':>8} {'live_ms':>8} {'liveKiB':>8} {'chat_ms':>8} {'mem_ms':>8}")
    for label, days in (("sample", 0), ("14d", 14), ("90d", 90), ("365d", 365)):
        m = measure_horizon(label, days)
        print(f"{m['horizon']:<8} {m['slots']:>7} {m['fac_ms']:>8} {m['lock_ms']:>8} {m['search_ms']:>8} "
              f"{m['wh_full_ms']:>8} {m['wh_full_kib']:>8} {m['wh_live_ms']:>8} {m['wh_live_kib']:>8} "
              f"{m['chats_ms']:>8} {m['mem_ms']:>8}")


if __name__ == "__main__":
    main()
