"""Phase 1: timestamp contract, repositories and named audit queries."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from setuhaul.db import TimestampError, open_connection, parse_operational_timestamp
from setuhaul.repositories import AUDIT_QUERIES, Repository, run_audit, timestamp_pattern_audit


@pytest.fixture()
def conn():
    c = open_connection(readonly=True)
    yield c
    c.close()


def test_parse_returns_aware_datetime_with_ist_offset() -> None:
    dt = parse_operational_timestamp("2026-08-04T09:00:00+05:30")
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(hours=5, minutes=30)


def test_parse_normalises_utc_to_operational_timezone() -> None:
    dt = parse_operational_timestamp("2026-08-04T03:30:00Z")
    assert dt == parse_operational_timestamp("2026-08-04T09:00:00+05:30")
    assert dt.utcoffset() == timedelta(hours=5, minutes=30)


@pytest.mark.parametrize("raw", ["2026-08-04T09:00:00", "2026-08-04 09:00:00", "", "garbage", "09:00"])
def test_parse_rejects_naive_or_invalid(raw: str) -> None:
    with pytest.raises(TimestampError):
        parse_operational_timestamp(raw)


def test_comparison_uses_instants_not_strings() -> None:
    utc_raw, ist_raw = "2026-08-04T04:00:00+00:00", "2026-08-04T09:00:00+05:30"  # 04:00Z vs 03:30Z
    assert utc_raw < ist_raw  # string order says UTC value is earlier...
    assert parse_operational_timestamp(utc_raw) > parse_operational_timestamp(ist_raw)  # ...it is later


def test_every_snapshot_timestamp_column_has_one_uniform_pattern(conn) -> None:
    result = timestamp_pattern_audit(conn)
    assert result.columns_checked >= 30
    assert result.mixed == {}
    assert result.naive == {}
    assert result.invalid == {}


def test_every_snapshot_timestamp_parses(conn) -> None:
    for (v,) in conn.execute("select slot_start_ts from appointment_slots"):
        assert isinstance(parse_operational_timestamp(v), datetime)


def test_named_audit_queries_present_and_clean(conn) -> None:
    report = run_audit(conn)
    assert set(report) == set(AUDIT_QUERIES) | {"foreign_key_violations", "integrity_check"}
    assert report["foreign_key_violations"] == []
    assert report["integrity_check"] == ["ok"]
    assert report["duplicate_active_slot"] == []
    assert report["duplicate_active_appointment_per_shipment"] == []
    assert report["orphan_appointments"] == []
    assert report["shipments_missing_feasibility_inputs"] == []


def test_audit_counts_match_snapshot(conn) -> None:
    c = run_audit(conn)["row_counts"]
    assert c["shipments"] == 21 and c["appointments"] == 20 and c["appointment_slots"] == 106


def test_audit_does_not_mutate_database() -> None:
    import hashlib

    from setuhaul.config import SEED_DB_SHA256, load_settings

    with open_connection(readonly=True) as c:
        run_audit(c)
    digest = hashlib.sha256(load_settings().db_path.read_bytes()).hexdigest().upper()
    assert digest == SEED_DB_SHA256


def test_readonly_connection_rejects_writes() -> None:
    import sqlite3

    with open_connection(readonly=True) as c, pytest.raises(sqlite3.OperationalError):
        c.execute("update shipments set priority_code='LOW'")


def test_writable_connection_uses_wal_and_busy_timeout(tmp_path) -> None:
    import shutil

    from setuhaul.config import load_settings

    copy = tmp_path / "copy.sqlite3"
    shutil.copy(load_settings().db_path, copy)
    c = open_connection(path=copy, readonly=False)
    try:
        assert c.execute("pragma journal_mode").fetchone()[0] == "wal"
        assert c.execute("pragma busy_timeout").fetchone()[0] == 5000
        assert c.execute("pragma foreign_keys").fetchone()[0] == 1
    finally:
        c.close()


def test_shipment_trace_end_to_end(conn) -> None:
    repo = Repository(conn)
    trace = repo.shipment_trace("SHP1001")
    assert set(trace) >= {"shipment", "driver", "vehicle", "eta", "facility", "appointment", "slot",
                          "dock", "checkin", "exceptions"}
    assert trace["shipment"]["shipment_id"] == "SHP1001"
    assert trace["driver"]["driver_id"] == trace["shipment"]["driver_id"]
    assert trace["vehicle"]["vehicle_id"] == trace["shipment"]["vehicle_id"]
    assert trace["facility"]["facility_id"] == trace["shipment"]["destination_facility_id"]


def test_repository_unknown_shipment_returns_none(conn) -> None:
    assert Repository(conn).get_shipment("NOPE") is None


def test_repository_emits_span_carrying_request_id(conn) -> None:
    from setuhaul.observability import InMemoryExporter, RequestContext

    exp, ctx = InMemoryExporter(), RequestContext.new(driver_id="D", thread_id="T")
    Repository(conn, ctx, exp).shipment_trace("SHP1001")
    assert [s.name for s in exp.spans] == ["repository.shipment_trace"]
    assert exp.spans[0].request_id == ctx.request_id


def test_trace_includes_appointment_slot_dock_and_history_for_active_shipment(conn) -> None:
    trace = Repository(conn).shipment_trace("SHP1006")
    assert trace["appointment"] is not None
    assert trace["slot"]["slot_id"] == trace["appointment"]["slot_id"]
    assert trace["dock"]["dock_id"] == trace["slot"]["dock_id"]
    assert len(trace["appointment_history"]) >= 1
