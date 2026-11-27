"""Data retention purges (D2) and per-driver erasure.

Retention purges are OFF by default (enable with ``SETUHAUL_RETENTION_ENABLED=1``) and run in bounded batches
as isolated maintenance steps. Every purge preserves foreign keys, open escalation cases, live holds and the
immutable appointment history.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from setuhaul.auth import audit
from setuhaul.clock import Clock
from setuhaul.db import TimestampError, immediate, parse_operational_timestamp


@dataclass(frozen=True)
class RetentionPolicy:
    """Retention periods in days (D2 defaults) and maximum rows processed per batch."""

    chat_days: int = 90
    gps_days: int = 30
    audit_ip_days: int = 90
    inbound_days: int = 30
    model_calls_days: int = 90
    batch_size: int = 500


@dataclass(frozen=True)
class ErasureReport:
    driver_id: str
    chat_messages_deleted: int
    gps_pings_deleted: int
    inbound_messages_deleted: int
    identities_deleted: int
    driver_scrubbed: bool
    retained_explanation: tuple[str, ...]


def _is_older(raw_ts: str, before: datetime) -> bool:
    try:
        return parse_operational_timestamp(raw_ts) < before
    except (TimestampError, ValueError):
        return False


def _protected_threads(conn: sqlite3.Connection) -> set[str]:
    """Threads referenced by an open/claimed escalation case or a live HELD appointment."""
    protected: set[str] = set()
    for r in conn.execute("select thread_id from escalation_cases where status in ('OPEN','CLAIMED')"):
        protected.add(str(r[0]))
    for r in conn.execute(
        "select m.thread_id from appointment_hold_meta m join appointments a using(appointment_id) "
        "where a.appointment_status='HELD'"
    ):
        protected.add(str(r[0]))
    return protected


def _stale_chat_ids(conn: sqlite3.Connection, before: datetime, limit: int | None = None) -> list[str]:
    protected = _protected_threads(conn)
    ids: list[str] = []
    for r in conn.execute("select chat_message_id, thread_id, message_ts from chat_messages order by rowid"):
        if str(r["thread_id"]) in protected:
            continue
        if _is_older(str(r["message_ts"]), before):
            ids.append(str(r["chat_message_id"]))
            if limit is not None and len(ids) >= limit:
                break
    return ids


def purge_chat_messages(conn: sqlite3.Connection, before: datetime, batch_size: int = 500) -> int:
    """Delete chat messages older than ``before`` (and cascaded chat_message_loads), skipping open cases and live holds."""
    ids = _stale_chat_ids(conn, before, limit=batch_size)
    if not ids:
        return 0
    with immediate(conn):
        conn.executemany("delete from chat_messages where chat_message_id=?", [(i,) for i in ids])
    return len(ids)


def _stale_gps_ids(conn: sqlite3.Connection, before: datetime, limit: int | None = None) -> list[int]:
    ids: list[int] = []
    for r in conn.execute("select ping_id, received_at from gps_pings order by ping_id"):
        if _is_older(str(r["received_at"]), before):
            ids.append(int(r["ping_id"]))
            if limit is not None and len(ids) >= limit:
                break
    return ids


def purge_gps_pings(conn: sqlite3.Connection, before: datetime, batch_size: int = 500) -> int:
    """Delete GPS pings received before ``before``, in bounded batches."""
    ids = _stale_gps_ids(conn, before, limit=batch_size)
    if not ids:
        return 0
    with immediate(conn):
        conn.executemany("delete from gps_pings where ping_id=?", [(i,) for i in ids])
    return len(ids)


def _stale_audit_ip_ids(conn: sqlite3.Connection, before: datetime, limit: int | None = None) -> list[int]:
    ids: list[int] = []
    for r in conn.execute("select audit_id, ts from audit_log where ip is not null order by audit_id"):
        if _is_older(str(r["ts"]), before):
            ids.append(int(r["audit_id"]))
            if limit is not None and len(ids) >= limit:
                break
    return ids


def purge_audit_log_ips(conn: sqlite3.Connection, before: datetime, batch_size: int = 500) -> int:
    """Scrub ``audit_log.ip`` to NULL for rows older than ``before`` (all other audit columns stay append-only)."""
    ids = _stale_audit_ip_ids(conn, before, limit=batch_size)
    if not ids:
        return 0
    with immediate(conn):
        conn.executemany("update audit_log set ip=NULL where audit_id=?", [(i,) for i in ids])
    return len(ids)


def _stale_inbound_keys(conn: sqlite3.Connection, before: datetime, limit: int | None = None) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    for r in conn.execute(
        "select driver_id, message_id, created_at from inbound_messages where status='DONE' order by claimed_epoch"
    ):
        if _is_older(str(r["created_at"]), before):
            keys.append((str(r["driver_id"]), str(r["message_id"])))
            if limit is not None and len(keys) >= limit:
                break
    return keys


def purge_inbound_messages(conn: sqlite3.Connection, before: datetime, batch_size: int = 500) -> int:
    """Delete completed inbound_messages rows older than ``before``. Never touches PROCESSING rows."""
    keys = _stale_inbound_keys(conn, before, limit=batch_size)
    if not keys:
        return 0
    with immediate(conn):
        conn.executemany(
            "delete from inbound_messages where driver_id=? and message_id=? and status='DONE'", keys
        )
    return len(keys)


def purge_model_calls(conn: sqlite3.Connection, before: datetime, batch_size: int = 500) -> int:
    """Delete model_calls (and then orphaned model_turns) older than ``before`` without breaking foreign keys."""
    cutoff_epoch = before.timestamp()
    call_ids = [
        str(r[0])
        for r in conn.execute(
            "select call_id from model_calls where ts_epoch < ? order by ts_epoch limit ?",
            (cutoff_epoch, batch_size),
        )
    ]
    turn_ids = [
        str(r[0])
        for r in conn.execute(
            "select t.turn_id from model_turns t where t.ts_epoch < ? "
            "and not exists (select 1 from model_calls c where c.turn_id = t.turn_id and c.ts_epoch >= ?) "
            "order by t.ts_epoch limit ?",
            (cutoff_epoch, cutoff_epoch, batch_size),
        )
    ]
    if not call_ids and not turn_ids:
        return 0
    with immediate(conn):
        if call_ids:
            conn.executemany("delete from model_calls where call_id=?", [(i,) for i in call_ids])
        if turn_ids:
            # Only delete turns that now have no remaining child model_calls.
            conn.executemany(
                "delete from model_turns where turn_id=? and not exists (select 1 from model_calls where turn_id=?)",
                [(t, t) for t in turn_ids],
            )
    return len(call_ids)


def preview_retention(
    conn: sqlite3.Connection, now: datetime, policy: RetentionPolicy = RetentionPolicy()
) -> dict[str, int]:
    """Read-only report of how many rows each retention purge would currently affect."""
    cutoff_model = (now - timedelta(days=policy.model_calls_days)).timestamp()
    model_calls_n = int(
        conn.execute("select count(*) from model_calls where ts_epoch < ?", (cutoff_model,)).fetchone()[0]
    )
    model_turns_n = int(
        conn.execute(
            "select count(*) from model_turns t where t.ts_epoch < ? "
            "and not exists (select 1 from model_calls c where c.turn_id = t.turn_id and c.ts_epoch >= ?)",
            (cutoff_model, cutoff_model),
        ).fetchone()[0]
    )
    return {
        "chat_messages": len(_stale_chat_ids(conn, now - timedelta(days=policy.chat_days))),
        "gps_pings": len(_stale_gps_ids(conn, now - timedelta(days=policy.gps_days))),
        "audit_log_ips": len(_stale_audit_ip_ids(conn, now - timedelta(days=policy.audit_ip_days))),
        "inbound_messages": len(_stale_inbound_keys(conn, now - timedelta(days=policy.inbound_days))),
        "model_calls": model_calls_n,
        "model_turns": model_turns_n,
    }


RETAINED_EXPLANATION: tuple[str, ...] = (
    "appointment_events: immutable append-only booking and dock history (database triggers forbid UPDATE/DELETE); holds no driver phone or licence.",
    "audit_log: append-only accountability trail (database triggers forbid DELETE); IP addresses are scrubbed after 90 days.",
    "shipments and appointments: operational/financial records retained for warehouse reconciliation; personal details are scrubbed from the linked drivers row.",
    "escalation_cases and escalation_events: closed case metadata retained for operational audit (case free-text summaries may require manual review if a driver typed personal details).",
)


def erase_driver_data(conn: sqlite3.Connection, driver_id: str, clock: Clock) -> ErasureReport:
    """Erase a driver's personal data and message/GPS history. Refuses if open cases, live holds, or active bookings exist."""
    with immediate(conn):
        exists = conn.execute("select 1 from drivers where driver_id=?", (driver_id,)).fetchone()
        if exists is None:
            raise ValueError(f"driver {driver_id!r} not found")
        open_cases = conn.execute(
            "select count(*) from escalation_cases where driver_id=? and status in ('OPEN','CLAIMED')", (driver_id,)
        ).fetchone()[0]
        if open_cases:
            raise ValueError(f"cannot erase driver {driver_id}: {open_cases} open escalation case(s)")
        active_appts = conn.execute(
            "select count(*) from appointments a join shipments s using(shipment_id) "
            "where s.driver_id=? and a.appointment_status in ('HELD','PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')",
            (driver_id,),
        ).fetchone()[0]
        if active_appts:
            raise ValueError(f"cannot erase driver {driver_id}: {active_appts} active appointment(s) or hold(s)")

        chat_del = conn.execute(
            "delete from chat_messages where thread_id in (select thread_id from chat_threads where driver_id=?)",
            (driver_id,),
        ).rowcount
        gps_del = conn.execute(
            "delete from gps_pings where shipment_id in (select shipment_id from shipments where driver_id=?)",
            (driver_id,),
        ).rowcount
        inb_del = conn.execute("delete from inbound_messages where driver_id=?", (driver_id,)).rowcount
        id_del = 0
        for sql in (
            "delete from telegram_identities where driver_id=?",
            "delete from channel_identities where driver_id=?",
            "delete from driver_sessions where driver_id=?",
            "delete from conversation_state where driver_id=?",
        ):
            id_del += conn.execute(sql, (driver_id,)).rowcount
        # Scrub PII in-place on drivers so foreign keys from shipments/appointments remain intact.
        conn.execute(
            "update drivers set driver_name='ERASED', phone=?, licence_number=?, home_base_city=NULL, "
            "driver_status='INACTIVE' where driver_id=?",
            (f"ERASED-{driver_id}", f"ERASED-{driver_id}", driver_id),
        )
        audit.record(
            conn,
            actor_user_id=None,
            actor_email=None,
            action="DRIVER_ERASED",
            target_type="driver",
            target_id=driver_id,
            outcome="OK",
            ip=None,
            detail={"chat_messages": chat_del, "gps_pings": gps_del, "inbound_messages": inb_del},
            clock=clock,
        )
    return ErasureReport(
        driver_id=driver_id,
        chat_messages_deleted=chat_del,
        gps_pings_deleted=gps_del,
        inbound_messages_deleted=inb_del,
        identities_deleted=id_del,
        driver_scrubbed=True,
        retained_explanation=RETAINED_EXPLANATION,
    )
