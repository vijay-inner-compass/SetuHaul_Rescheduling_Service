"""Batch 6 tests: F08 capacity window/no truncation/live fragment, F14 secret form methods, F15 migration FK rollback, F16 distinct TOO_SOON codes."""

from __future__ import annotations

import re
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

from setuhaul.api import AppDeps, create_app
from setuhaul.assign import open_slots, open_slots_page
from setuhaul.auth import sessions, users
from setuhaul.capacity import build_facility_state
from setuhaul.clock import FakeClock
from setuhaul.config import load_settings
from setuhaul.db import MIGRATIONS_DIR, apply_migrations, open_connection

COMMON_JS = Path(__file__).resolve().parents[1] / "src" / "setuhaul" / "auth" / "assets" / "common.js"


# ---- F08 ---------------------------------------------------------------------------------------------------

def test_build_facility_state_window_keeps_live_hold_and_drops_old_history(db: sqlite3.Connection, clock: FakeClock) -> None:
    fac = "FAC-JAI-01"
    dock = db.execute("select dock_id from docks where facility_id=? limit 1", (fac,)).fetchone()[0]
    old_start = (clock.now() - timedelta(days=120)).isoformat()
    old_end = (clock.now() - timedelta(days=120) + timedelta(hours=1)).isoformat()
    far_start = (clock.now() + timedelta(days=60)).isoformat()
    far_end = (clock.now() + timedelta(days=60) + timedelta(hours=1)).isoformat()
    stamp = clock.now().isoformat()

    # 120-day-old unused slot -> should NOT be loaded into FacilityState.
    db.execute(
        "insert into appointment_slots (slot_id, facility_id, dock_id, slot_start_ts, slot_end_ts, slot_status, created_at) "
        "values ('SLOT-ANCIENT', ?, ?, ?, ?, 'OPEN', ?)",
        (fac, dock, old_start, old_end, stamp),
    )
    # 60-day-ahead slot WITH an active booking -> MUST still be loaded!
    db.execute(
        "insert into appointment_slots (slot_id, facility_id, dock_id, slot_start_ts, slot_end_ts, slot_status, created_at) "
        "values ('SLOT-FAR-BOOKED', ?, ?, ?, ?, 'OPEN', ?)",
        (fac, dock, far_start, far_end, stamp),
    )
    db.execute(
        "insert into appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source, is_current, booked_at, updated_at) "
        "values ('APT-FAR', 'SHP1006', 'SLOT-FAR-BOOKED', 'CONFIRMED', 'WAREHOUSE', 0, ?, ?)",
        (stamp, stamp),
    )
    # Open-ended dock block started 100 days ago -> MUST still be loaded!
    db.execute(
        "insert into dock_status_events (dock_event_id, dock_id, event_type, event_start_ts, event_end_ts, reason, created_at) "
        "values ('DEV-OPEN-ENDED', ?, 'MAINTENANCE', ?, null, 'long repair', ?)",
        (dock, old_start, stamp),
    )
    db.commit()

    state = build_facility_state(db, fac, clock.now())
    assert "SLOT-ANCIENT" not in state.slots
    assert "SLOT-FAR-BOOKED" in state.slots
    assert any(e.reason == "long repair" for e in state.events)


def test_open_slots_does_not_silently_truncate_beyond_3000(db: sqlite3.Connection, clock: FakeClock) -> None:
    fac = "FAC-JAI-01"
    dock = db.execute("select dock_id from docks where facility_id=? limit 1", (fac,)).fetchone()[0]
    stamp = clock.now().isoformat()
    base = clock.now() + timedelta(hours=2)
    rows = [
        (
            f"S3K-{i:04d}",
            fac,
            dock,
            (base + timedelta(minutes=i * 5)).isoformat(),
            (base + timedelta(minutes=i * 5 + 4)).isoformat(),
            "OPEN",
            stamp,
        )
        for i in range(3010)
    ]
    db.executemany(
        "insert into appointment_slots (slot_id, facility_id, dock_id, slot_start_ts, slot_end_ts, slot_status, created_at) "
        "values (?,?,?,?,?,?,?)",
        rows,
    )
    db.commit()

    all_free = open_slots(db, fac, clock.now())
    assert len(all_free) >= 3010
    page, has_more = open_slots_page(db, fac, clock.now(), limit=200)
    assert len(page) == 200 and has_more is True


def test_warehouse_open_slots_endpoint_and_live_fragment(working_db_path: Path, clock: FakeClock) -> None:
    conn = open_connection(path=working_db_path)
    try:
        admin = users.bootstrap_admin(conn, "admin@x.com", clock)
        assert admin is not None
        wh = users.invite(conn, actor=admin, email="wh@x.com", role="WAREHOUSE_AUTHORITY",
                          display_name="WH", facilities=("FAC-JAI-01",), clock=clock)
        token = sessions.create(conn, wh, "GOOGLE", clock)
        conn.commit()
    finally:
        conn.close()

    deps = AppDeps(
        db_path=working_db_path,
        redis=fakeredis.FakeRedis(decode_responses=True),
        webhook_secret=b"s",
        coordinator_tokens={},
        fallback_path=working_db_path.parent / "fb.jsonl",
        clock=clock,
    )
    client = TestClient(create_app(deps))
    client.cookies.set(sessions.COOKIE_NAME, token)

    # In-scope facility succeeds; out-of-scope facility returns 404.
    r_ok = client.get("/v1/warehouse/open-slots", params={"facility_id": "FAC-JAI-01"})
    assert r_ok.status_code == 200 and "slots" in r_ok.json() and "has_more" in r_ok.json()
    r_out = client.get("/v1/warehouse/open-slots", params={"facility_id": "FAC-GGN-01"})
    assert r_out.status_code == 404

    # X-Live: 1 returns a smaller fragment containing the live regions.
    r_full = client.get("/warehouse", params={"facility": "FAC-JAI-01"})
    r_live = client.get("/warehouse", params={"facility": "FAC-JAI-01"}, headers={"X-Live": "1"})
    assert r_full.status_code == 200 and r_live.status_code == 200
    assert len(r_live.content) < len(r_full.content)
    assert 'id="board"' in r_live.text and 'id="messages"' in r_live.text
    assert 'id="shipments"' not in r_live.text


# ---- F14 ---------------------------------------------------------------------------------------------------

def test_all_secret_forms_use_post_and_non_script_submission_fails_safely(working_db_path: Path, clock: FakeClock) -> None:
    conn = open_connection(path=working_db_path)
    try:
        admin = users.bootstrap_admin(conn, "admin@x.com", clock)
        assert admin is not None
        token = sessions.create(conn, admin, "GOOGLE", clock)
        conn.commit()
    finally:
        conn.close()

    deps = AppDeps(
        db_path=working_db_path,
        redis=fakeredis.FakeRedis(decode_responses=True),
        webhook_secret=b"s",
        coordinator_tokens={},
        fallback_path=working_db_path.parent / "fb.jsonl",
        clock=clock,
        local_admin_enabled=True,
    )
    client = TestClient(create_app(deps))

    # Landing page with local-admin form.
    landing = client.get("/")
    assert landing.status_code == 200
    secret_forms = [m.group(0) for m in re.finditer(r"<form\b[^>]*>.*?</form>", landing.text, re.DOTALL) if 'type="password"' in m.group(0)]
    assert secret_forms, "expected at least one password form on /"
    for form in secret_forms:
        assert re.search(r'<form\b[^>]*\bmethod="post"', form, re.IGNORECASE), form[:120]

    # Admin page with settings forms carrying secret inputs.
    client.cookies.set(sessions.COOKIE_NAME, token)
    admin_html = client.get("/admin")
    assert admin_html.status_code == 200
    admin_secret_forms = [m.group(0) for m in re.finditer(r"<form\b[^>]*>.*?</form>", admin_html.text, re.DOTALL) if 'type="password"' in m.group(0)]
    assert admin_secret_forms, "expected password forms on /admin"
    for form in admin_secret_forms:
        assert re.search(r'<form\b[^>]*\bmethod="post"', form, re.IGNORECASE), form[:120]

    # Non-script POST submission fails safely (405 Method Not Allowed) without echoing the secret.
    secret_val = "super-secret-value-123"
    r_post_root = client.post("/", data={"email": "admin@x.com", "password": secret_val})
    assert r_post_root.status_code == 405 and secret_val not in r_post_root.text
    r_post_admin = client.post("/admin", data={"api_key": secret_val})
    assert r_post_admin.status_code == 405 and secret_val not in r_post_admin.text


# ---- F15 ---------------------------------------------------------------------------------------------------

def test_bad_migration_with_fk_violation_rolls_back_and_is_not_recorded(tmp_path: Path) -> None:
    db_path = tmp_path / "m.db"
    shutil.copyfile(load_settings().db_path, db_path)
    mig_dir = tmp_path / "migs"
    mig_dir.mkdir()
    for p in sorted(MIGRATIONS_DIR.glob("*.sql")):
        shutil.copyfile(p, mig_dir / p.name)
    # Add a bad migration that creates a table and inserts a row with a broken foreign key.
    (mig_dir / "999_bad_fk.sql").write_text(
        "CREATE TABLE bad_fk_child (id INTEGER PRIMARY KEY, driver_id TEXT NOT NULL REFERENCES drivers(driver_id));\n"
        "INSERT INTO bad_fk_child (id, driver_id) VALUES (1, 'NO-SUCH-DRIVER');\n",
        encoding="utf-8",
    )
    conn = open_connection(path=db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="foreign key violations"):
            apply_migrations(conn, mig_dir)
        # Rolled back: table does not exist and migration is NOT recorded in schema_migrations.
        tbl = conn.execute("select 1 from sqlite_master where type='table' and name='bad_fk_child'").fetchone()
        rec = conn.execute("select 1 from schema_migrations where filename='999_bad_fk.sql'").fetchone()
        assert tbl is None
        assert rec is None
        assert conn.execute("pragma foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_all_migrations_apply_cleanly_to_fresh_and_populated_copies(tmp_path: Path, clock: FakeClock) -> None:
    # 1. Fresh copy of seed database.
    fresh = tmp_path / "fresh.db"
    shutil.copyfile(load_settings().db_path, fresh)
    c1 = open_connection(path=fresh)
    try:
        apply_migrations(c1)
        assert c1.execute("pragma foreign_key_check").fetchall() == []
        assert [tuple(r) for r in c1.execute("pragma integrity_check").fetchall()] == [("ok",)]
    finally:
        c1.close()

    # 2. Populated copy (migrated up to 023, populated with rows, then migrated forward to latest).
    pop = tmp_path / "populated.db"
    shutil.copyfile(load_settings().db_path, pop)
    pre_dir = tmp_path / "pre"
    pre_dir.mkdir()
    for p in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if p.name <= "023_driver_notices_without_event.sql":
            shutil.copyfile(p, pre_dir / p.name)
    c2 = open_connection(path=pop)
    try:
        apply_migrations(c2, pre_dir)
        admin = users.bootstrap_admin(c2, "pop@x.com", clock)
        assert admin is not None
        c2.commit()
        applied = apply_migrations(c2)
        assert "024_auth_ip_lockouts.sql" in applied
        assert "025_audit_ip_retention.sql" in applied
        assert "026_capacity_window_indexes.sql" in applied
        assert c2.execute("pragma foreign_key_check").fetchall() == []
        assert [tuple(r) for r in c2.execute("pragma integrity_check").fetchall()] == [("ok",)]
    finally:
        c2.close()


# ---- F16 ---------------------------------------------------------------------------------------------------

def test_common_js_has_no_duplicate_keys_and_distinct_too_soon_messages() -> None:
    text = COMMON_JS.read_text(encoding="utf-8")
    block = re.search(r"var MESSAGES = \{(.*?)\n  \};", text, re.DOTALL)
    assert block is not None
    keys = re.findall(r'^\s+"?([A-Z_]{4,})"?: ', block.group(1), re.MULTILINE)
    duplicates = [k for k in set(keys) if keys.count(k) > 1]
    assert duplicates == [], f"duplicate MESSAGES keys in common.js: {duplicates}"
    assert "EMAIL_TEST_TOO_SOON" in keys
    assert "PASSWORD_LINK_TOO_SOON" in keys
    assert "TOO_SOON" not in keys
