"""Batch 5 F13: retention purges (D2) and per-driver erasure."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from setuhaul.auth import audit
from setuhaul.clock import FakeClock
from setuhaul.jobs.expire_holds import MaintenanceRunner
from setuhaul.retention import (
    RetentionPolicy,
    erase_driver_data,
    preview_retention,
    purge_audit_log_ips,
    purge_chat_messages,
    purge_gps_pings,
    purge_inbound_messages,
    purge_model_calls,
)


def _driver_and_shipment(db: sqlite3.Connection) -> tuple[str, str]:
    row = db.execute(
        "select s.driver_id, s.shipment_id from shipments s join drivers d using(driver_id) order by s.shipment_id limit 1"
    ).fetchone()
    return str(row["driver_id"]), str(row["shipment_id"])


def test_purge_chat_messages_removes_old_and_keeps_recent_open_case_and_live_hold(
    db: sqlite3.Connection, clock: FakeClock
) -> None:
    drv, shp = _driver_and_shipment(db)
    old_ts = (clock.now() - timedelta(days=95)).isoformat()
    recent_ts = (clock.now() - timedelta(days=10)).isoformat()

    # Thread 1: old, no open case, no live hold -> old msg deleted, recent msg kept.
    db.execute(
        "insert into chat_threads (thread_id, driver_id, shipment_id, opened_at, thread_status, thread_intent) "
        "values ('THR-OLD', ?, ?, ?, 'RESOLVED', 'REPORT_DELAY')",
        (drv, shp, old_ts),
    )
    db.execute(
        "insert into chat_messages (chat_message_id, thread_id, sender_type, message_text, message_ts) "
        "values ('MSG-OLD-1', 'THR-OLD', 'DRIVER', 'old text', ?), ('MSG-NEW-1', 'THR-OLD', 'DRIVER', 'new text', ?)",
        (old_ts, recent_ts),
    )

    # Thread 2: old, but has an OPEN escalation case -> kept!
    db.execute(
        "insert into chat_threads (thread_id, driver_id, shipment_id, opened_at, thread_status, thread_intent) "
        "values ('THR-CASE', ?, ?, ?, 'ESCALATED', 'REPORT_DELAY')",
        (drv, shp, old_ts),
    )
    db.execute(
        "insert into chat_messages (chat_message_id, thread_id, sender_type, message_text, message_ts) "
        "values ('MSG-CASE-1', 'THR-CASE', 'DRIVER', 'case text', ?)",
        (old_ts,),
    )
    db.execute(
        "insert into escalation_cases (case_id, dedupe_key, status, reason_code, driver_id, thread_id, shipment_id, "
        "summary, created_at, updated_at) values ('CASE-OPEN', 'dk1', 'OPEN', 'NO_SLOT', ?, 'THR-CASE', ?, 's', ?, ?)",
        (drv, shp, old_ts, old_ts),
    )

    # Thread 3: old, but referenced by a live HELD appointment -> kept!
    slot_row = db.execute(
        "select slot_id, facility_id from appointment_slots where slot_status='OPEN' "
        "and slot_id not in (select slot_id from appointments where appointment_status in ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS','HELD')) "
        "order by slot_start_ts limit 1"
    ).fetchone()
    db.execute(
        "insert into chat_threads (thread_id, driver_id, shipment_id, opened_at, thread_status, thread_intent) "
        "values ('THR-HOLD', ?, ?, ?, 'OPEN', 'REPORT_DELAY')",
        (drv, shp, old_ts),
    )
    db.execute(
        "insert into chat_messages (chat_message_id, thread_id, sender_type, message_text, message_ts) "
        "values ('MSG-HOLD-1', 'THR-HOLD', 'DRIVER', 'hold text', ?)",
        (old_ts,),
    )
    exp = clock.now() + timedelta(minutes=10)
    db.execute(
        "insert into option_sets (option_set_id, driver_id, thread_id, shipment_id, facility_id, shown_at, expires_at, "
        "rules_version, context_json) values ('OPT-LIVE', ?, 'THR-HOLD', ?, ?, ?, ?, 'v1', '{}')",
        (drv, shp, slot_row["facility_id"], old_ts, exp.isoformat()),
    )
    db.execute(
        "insert into appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source, is_current, "
        "booked_at, updated_at) values ('APT-LIVE', ?, ?, 'HELD', 'DRIVER_CHAT', 0, ?, ?)",
        (shp, slot_row["slot_id"], old_ts, old_ts),
    )
    db.execute(
        "insert into appointment_hold_meta (appointment_id, driver_id, thread_id, option_set_id, expires_at, "
        "expires_epoch, request_fingerprint) values ('APT-LIVE', ?, 'THR-HOLD', 'OPT-LIVE', ?, ?, 'fp')",
        (drv, exp.isoformat(), int(exp.timestamp())),
    )
    db.commit()

    removed = purge_chat_messages(db, clock.now() - timedelta(days=90), batch_size=10)
    assert removed >= 1
    remaining = {r[0] for r in db.execute("select chat_message_id from chat_messages").fetchall()}
    assert "MSG-OLD-1" not in remaining
    assert {"MSG-NEW-1", "MSG-CASE-1", "MSG-HOLD-1"}.issubset(remaining)
    assert db.execute("pragma foreign_key_check").fetchall() == []


def test_purge_gps_pings_removes_old_and_keeps_recent(db: sqlite3.Connection, clock: FakeClock) -> None:
    _, shp = _driver_and_shipment(db)
    old_ts = (clock.now() - timedelta(days=35)).isoformat()
    recent_ts = (clock.now() - timedelta(days=5)).isoformat()
    db.execute(
        "insert into gps_pings (shipment_id, lat, lon, recorded_at, received_at, source) "
        "values (?, 28.5, 77.0, ?, ?, 'TELEMATICS'), (?, 28.6, 77.1, ?, ?, 'TELEMATICS')",
        (shp, old_ts, old_ts, shp, recent_ts, recent_ts),
    )
    db.commit()

    removed = purge_gps_pings(db, clock.now() - timedelta(days=30), batch_size=10)
    assert removed == 1
    rows = db.execute("select received_at from gps_pings").fetchall()
    assert [r[0] for r in rows] == [recent_ts]


def test_purge_audit_log_ips_scrubs_old_ips_and_still_blocks_tampering(db: sqlite3.Connection, clock: FakeClock) -> None:
    old_clock = FakeClock(clock.now() - timedelta(days=95))
    audit.record(db, actor_user_id=None, actor_email="a@x.com", action="LOGIN", target_type=None, target_id=None,
                 outcome="OK", ip="203.0.113.10", detail=None, clock=old_clock)
    audit.record(db, actor_user_id=None, actor_email="b@x.com", action="LOGIN", target_type=None, target_id=None,
                 outcome="OK", ip="203.0.113.20", detail=None, clock=clock)
    db.commit()

    scrubbed = purge_audit_log_ips(db, clock.now() - timedelta(days=90), batch_size=10)
    assert scrubbed == 1
    rows = {r["actor_email"]: r["ip"] for r in db.execute("select actor_email, ip from audit_log").fetchall()}
    assert rows["a@x.com"] is None
    assert rows["b@x.com"] == "203.0.113.20"

    # Tampering with any non-IP column or setting IP to a non-null value is still forbidden by the trigger!
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("update audit_log set outcome='TAMPERED' where actor_email='b@x.com'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("update audit_log set ip='1.1.1.1' where actor_email='b@x.com'")


def test_purge_inbound_messages_removes_old_done_and_keeps_recent_and_processing(
    db: sqlite3.Connection, clock: FakeClock
) -> None:
    drv, _ = _driver_and_shipment(db)
    old_dt = clock.now() - timedelta(days=35)
    recent_dt = clock.now() - timedelta(days=5)
    db.execute(
        "insert into inbound_messages (driver_id, message_id, fingerprint, status, reply_json, claimed_epoch, created_at, completed_at) "
        "values (?, 'm-old-done', 'fp1', 'DONE', '{}', ?, ?, ?), "
        "(?, 'm-old-proc', 'fp2', 'PROCESSING', null, ?, ?, null), "
        "(?, 'm-new-done', 'fp3', 'DONE', '{}', ?, ?, ?)",
        (drv, int(old_dt.timestamp()), old_dt.isoformat(), old_dt.isoformat(),
         drv, int(old_dt.timestamp()), old_dt.isoformat(),
         drv, int(recent_dt.timestamp()), recent_dt.isoformat(), recent_dt.isoformat()),
    )
    db.commit()

    removed = purge_inbound_messages(db, clock.now() - timedelta(days=30), batch_size=10)
    assert removed == 1
    ids = {r[0] for r in db.execute("select message_id from inbound_messages").fetchall()}
    assert ids == {"m-old-proc", "m-new-done"}


def test_purge_model_calls_removes_old_and_keeps_recent_without_breaking_fk(
    db: sqlite3.Connection, clock: FakeClock
) -> None:
    old_dt = clock.now() - timedelta(days=95)
    recent_dt = clock.now() - timedelta(days=10)
    db.execute(
        "insert into model_turns (turn_id, ts, ts_epoch, duration_ms, status) values "
        "('T-OLD', ?, ?, 100.0, 'OK'), ('T-NEW', ?, ?, 100.0, 'OK')",
        (old_dt.isoformat(), old_dt.timestamp(), recent_dt.isoformat(), recent_dt.timestamp()),
    )
    db.execute(
        "insert into model_calls (call_id, turn_id, ts, ts_epoch, provider, model_id, duration_ms, outcome, input_tokens, output_tokens) "
        "values ('C-OLD', 'T-OLD', ?, ?, 'openai', 'gpt', 50.0, 'OK', 10, 5), "
        "('C-NEW', 'T-NEW', ?, ?, 'openai', 'gpt', 50.0, 'OK', 10, 5)",
        (old_dt.isoformat(), old_dt.timestamp(), recent_dt.isoformat(), recent_dt.timestamp()),
    )
    db.commit()

    removed = purge_model_calls(db, clock.now() - timedelta(days=90), batch_size=10)
    assert removed == 1
    assert [r[0] for r in db.execute("select call_id from model_calls").fetchall()] == ["C-NEW"]
    assert [r[0] for r in db.execute("select turn_id from model_turns").fetchall()] == ["T-NEW"]
    assert db.execute("pragma foreign_key_check").fetchall() == []


def test_retention_off_by_default_in_maintenance_runner_and_preview_matches_actual(
    db: sqlite3.Connection, clock: FakeClock
) -> None:
    _, shp = _driver_and_shipment(db)
    old_ts = (clock.now() - timedelta(days=35)).isoformat()
    db.execute(
        "insert into gps_pings (shipment_id, lat, lon, recorded_at, received_at, source) values (?, 28.5, 77.0, ?, ?, 'TELEMATICS')",
        (shp, old_ts, old_ts),
    )
    db.commit()

    # Off by default: MaintenanceRunner does not touch gps_pings.
    MaintenanceRunner(db, clock).run_once()
    assert db.execute("select count(*) from gps_pings").fetchone()[0] == 1

    # Dry-run preview reports exact counts without deleting anything.
    preview = preview_retention(db, clock.now(), RetentionPolicy())
    assert preview["gps_pings"] == 1
    assert db.execute("select count(*) from gps_pings").fetchone()[0] == 1

    # Enabled explicitly: MaintenanceRunner purges it in an isolated step.
    MaintenanceRunner(db, clock, retention_enabled=True).run_once()
    assert db.execute("select count(*) from gps_pings").fetchone()[0] == 0


def test_erase_driver_data_scrubs_pii_preserves_history_and_refuses_open_case(
    db: sqlite3.Connection, clock: FakeClock
) -> None:
    drv, shp = _driver_and_shipment(db)
    now_iso = clock.now().isoformat()
    # End any active appointments on this driver's shipments so erasure is allowed once no open case exists.
    db.execute(
        "update appointments set appointment_status='COMPLETED', is_current=0 where shipment_id in "
        "(select shipment_id from shipments where driver_id=?)",
        (drv,),
    )
    db.execute(
        "insert into chat_threads (thread_id, driver_id, shipment_id, opened_at, thread_status, thread_intent) "
        "values ('THR-E', ?, ?, ?, 'OPEN', 'REPORT_DELAY')",
        (drv, shp, now_iso),
    )
    db.execute(
        "insert into chat_messages (chat_message_id, thread_id, sender_type, message_text, message_ts) "
        "values ('MSG-E1', 'THR-E', 'DRIVER', 'secret phone 9999999999', ?)",
        (now_iso,),
    )
    db.execute(
        "insert into gps_pings (shipment_id, lat, lon, recorded_at, received_at, source) values (?, 28.5, 77.0, ?, ?, 'DRIVER_SHARED')",
        (shp, now_iso, now_iso),
    )
    db.execute(
        "insert into escalation_cases (case_id, dedupe_key, status, reason_code, driver_id, thread_id, shipment_id, "
        "summary, created_at, updated_at) values ('CASE-E', 'dke', 'OPEN', 'NO_SLOT', ?, 'THR-E', ?, 's', ?, ?)",
        (drv, shp, now_iso, now_iso),
    )
    db.commit()

    # Refused while an open escalation case exists.
    with pytest.raises(ValueError, match="open escalation"):
        erase_driver_data(db, drv, clock)

    # Resolve the case, then erase succeeds.
    db.execute(
        "update escalation_cases set status='RESOLVED', resolved_at=?, resolution_code='HANDLED' where case_id='CASE-E'",
        (now_iso,),
    )
    db.commit()
    report = erase_driver_data(db, drv, clock)
    assert report.chat_messages_deleted >= 1
    assert report.gps_pings_deleted == 1
    assert report.driver_scrubbed is True
    assert len(report.retained_explanation) >= 2

    row = db.execute("select driver_name, phone, licence_number, driver_status from drivers where driver_id=?", (drv,)).fetchone()
    assert row["driver_name"] == "ERASED"
    assert row["phone"] == f"ERASED-{drv}"
    assert row["licence_number"] == f"ERASED-{drv}"
    assert row["driver_status"] == "INACTIVE"
    assert db.execute("select count(*) from gps_pings where shipment_id=?", (shp,)).fetchone()[0] == 0
    assert db.execute("pragma foreign_key_check").fetchall() == []
