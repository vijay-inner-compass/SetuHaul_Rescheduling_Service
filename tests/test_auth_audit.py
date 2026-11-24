"""Audit log: round trip, append-only triggers, and secret scrubbing."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import json
import sqlite3

import pytest

from setuhaul.auth import audit
from setuhaul.clock import FakeClock


def _record(db: sqlite3.Connection, clock: FakeClock, **over: object) -> None:
    # Defaults describe a successful admin login; tests override only what they care about.
    args: dict[str, object] = {
        "actor_user_id": "U1",
        "actor_email": "a@x.com",
        "action": "LOGIN_OK",
        "target_type": "user",
        "target_id": "U1",
        "outcome": "OK",
        "ip": "10.0.0.1",
        "detail": {"method": "GOOGLE"},
    }
    args.update(over)
    audit.record(db, clock=clock, **args)  # type: ignore[arg-type]


def test_record_then_query_roundtrip(db: sqlite3.Connection, clock: FakeClock) -> None:
    # Write one row and read it back through the filters.
    _record(db, clock)
    _record(db, clock, actor_user_id=None, actor_email="b@x.com", action="LOGIN_REFUSED", outcome="DENIED")
    rows = audit.query(db)
    assert len(rows) == 2
    # Newest first; the second write comes back first.
    assert rows[0]["action"] == "LOGIN_REFUSED"
    assert json.loads(rows[1]["detail_json"]) == {"method": "GOOGLE"}
    assert rows[1]["ts"].startswith("2026-08-04T09:35:00")
    # Filters narrow by actor (id or email), action and outcome.
    assert len(audit.query(db, actor="b@x.com")) == 1
    assert len(audit.query(db, actor="U1")) == 1
    assert len(audit.query(db, action="LOGIN_OK")) == 1
    assert len(audit.query(db, outcome="DENIED")) == 1
    assert audit.query(db, since="2027-01-01T00:00:00+05:30") == []
    assert len(audit.query(db, limit=1)) == 1


def test_update_is_rejected(db: sqlite3.Connection, clock: FakeClock) -> None:
    _record(db, clock)
    # The trigger aborts any UPDATE.
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE audit_log SET outcome = 'X'")


def test_delete_is_rejected(db: sqlite3.Connection, clock: FakeClock) -> None:
    _record(db, clock)
    # The trigger aborts any DELETE.
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM audit_log")


def test_detail_never_stores_secret_keys(db: sqlite3.Connection, clock: FakeClock) -> None:
    # Secret-looking keys are dropped, case-insensitively and in nested dicts; other keys survive.
    _record(db, clock, detail={"password": "p", "Token": "t", "code": "c", "why": "ok", "n": {"password": "p", "k": 1}})
    stored = json.loads(audit.query(db)[0]["detail_json"])
    assert stored == {"why": "ok", "n": {"k": 1}}


def test_users_role_check_and_email_nocase(db: sqlite3.Connection) -> None:
    # Only the two web roles are allowed.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO users(user_id,email,role,status,created_at) VALUES ('u','a@x.com','DRIVER','ACTIVE','t')"
        )
    db.execute("INSERT INTO users(user_id,email,role,status,created_at) VALUES ('u1','Vijay@X.com','ADMIN','ACTIVE','t')")
    # Email uniqueness ignores case.
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO users(user_id,email,role,status,created_at) VALUES ('u2','vijay@x.com','ADMIN','ACTIVE','t')"
        )
