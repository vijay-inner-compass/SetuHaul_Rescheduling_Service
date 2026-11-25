"""Break-glass local admin password: off by default, scrypt, lockout, audited."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import re
import threading
from datetime import timedelta
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

from setuhaul.api import AppDeps, create_app
from setuhaul.auth import audit, local_admin, users
from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection
from setuhaul.identity import AuthError

PASSWORD = "correct horse battery"


def _make(tmp_path: Path, clock: FakeClock, enabled: bool = True):  # type: ignore[no-untyped-def]
    # A database with one admin (with a password) and one warehouse authority (also with a password).
    path = make_working_db(tmp_path / "la.sqlite3")
    conn = open_connection(path=path)
    admin = users.bootstrap_admin(conn, "boss@x.com", clock)
    wh = users.invite(conn, actor=admin, email="wh@x.com", role="ADMIN", display_name="W", clock=clock)
    # Demote the second user directly so it is a non-admin holding a credential row.
    conn.execute("update users set role='WAREHOUSE_AUTHORITY' where user_id=?", (wh.user_id,))
    conn.execute("insert into user_facilities(user_id, facility_id) select ?, facility_id from facilities limit 1",
                 (wh.user_id,))
    conn.commit()
    local_admin.set_password(conn, admin.user_id, PASSWORD)
    local_admin.set_password(conn, wh.user_id, PASSWORD)
    conn.close()
    deps = AppDeps(db_path=path, redis=fakeredis.FakeRedis(decode_responses=True), webhook_secret=b"w",
                   coordinator_tokens={}, fallback_path=tmp_path / "f.jsonl", clock=clock,
                   session_secret=b"s" * 32, local_admin_enabled=enabled)
    return path, TestClient(create_app(deps), base_url="http://localhost", follow_redirects=False)


def _login(client: TestClient, email: str = "boss@x.com", password: str = PASSWORD):  # type: ignore[no-untyped-def]
    return client.post("/auth/local-admin", json={"email": email, "password": password})


def _rows(path: Path):  # type: ignore[no-untyped-def]
    conn = open_connection(path=path, readonly=True)
    try:
        return [r for r in audit.query(conn) if r["outcome"].startswith("LOCAL_ADMIN")]
    finally:
        conn.close()


def test_disabled_by_default(tmp_path: Path, clock: FakeClock) -> None:
    _, client = _make(tmp_path, clock, enabled=False)
    assert _login(client).status_code == 404


def test_correct_password_gives_one_hour_session(tmp_path: Path, clock: FakeClock) -> None:
    _, client = _make(tmp_path, clock)
    r = _login(client)
    assert r.status_code == 200
    assert client.get("/auth/me").json()["email"] == "boss@x.com"
    # The session is only an hour long.
    clock.advance(timedelta(minutes=61))
    assert client.get("/auth/me").status_code == 401


def test_five_failures_lock_for_15_minutes_then_unlock(tmp_path: Path, clock: FakeClock) -> None:
    _, client = _make(tmp_path, clock)
    for _ in range(5):
        r = _login(client, password="wrong password!!")
        assert r.status_code == 401 and r.json()["reason_code"] == "BAD_CREDENTIALS"
    # Locked now, even with the right password.
    # The route answers a locked account exactly like a wrong password (no enumeration).
    r = _login(client)
    assert r.status_code == 401 and r.json()["reason_code"] == "BAD_CREDENTIALS"
    clock.advance(timedelta(minutes=14))
    assert _login(client).status_code == 401
    clock.advance(timedelta(minutes=2))
    assert _login(client).status_code == 200


def test_success_resets_failure_counter(tmp_path: Path, clock: FakeClock) -> None:
    _, client = _make(tmp_path, clock)
    for _ in range(4):
        _login(client, password="wrong password!!")
    assert _login(client).status_code == 200
    # The counter started over, so four more failures do not lock.
    for _ in range(4):
        _login(client, password="wrong password!!")
    assert _login(client).status_code == 200


def test_non_admin_cannot_use_it(tmp_path: Path, clock: FakeClock) -> None:
    _, client = _make(tmp_path, clock)
    r = _login(client, email="wh@x.com")
    assert r.status_code == 401 and r.json()["reason_code"] == "BAD_CREDENTIALS"
    assert client.get("/auth/me").status_code == 401


def test_unknown_email_looks_like_wrong_password(tmp_path: Path, clock: FakeClock) -> None:
    _, client = _make(tmp_path, clock)
    r = _login(client, email="nobody@x.com")
    assert r.status_code == 401 and r.json()["reason_code"] == "BAD_CREDENTIALS"


def test_disabled_admin_refused(tmp_path: Path, clock: FakeClock) -> None:
    path, client = _make(tmp_path, clock)
    conn = open_connection(path=path)
    conn.execute("update users set status='DISABLED' where email='boss@x.com'")
    conn.commit()
    conn.close()
    assert _login(client).status_code == 401


def test_every_attempt_audited(tmp_path: Path, clock: FakeClock) -> None:
    path, client = _make(tmp_path, clock)
    assert _login(client).status_code == 200
    for _ in range(5):
        _login(client, password="wrong password!!")
    _login(client)  # locked
    outcomes = [r["outcome"] for r in _rows(path)]
    assert outcomes.count("LOCAL_ADMIN_OK") == 1
    assert outcomes.count("LOCAL_ADMIN_REFUSED") == 5
    assert outcomes.count("LOCAL_ADMIN_LOCKED") == 1


def test_no_password_or_hash_in_audit_detail(tmp_path: Path, clock: FakeClock) -> None:
    path, client = _make(tmp_path, clock)
    _login(client, password="wrong password!!")
    _login(client)
    conn = open_connection(path=path, readonly=True)
    stored = conn.execute("select password_hash, salt from admin_credentials").fetchall()
    dump = " ".join(str(dict(r)) for r in conn.execute("select * from audit_log").fetchall())
    conn.close()
    assert "wrong password!!" not in dump and PASSWORD not in dump
    for row in stored:
        assert row["password_hash"] not in dump and row["salt"] not in dump


def test_set_password_rejects_short_and_uses_per_user_salt(tmp_path: Path, clock: FakeClock) -> None:
    path = make_working_db(tmp_path / "s.sqlite3")
    conn = open_connection(path=path)
    a = users.bootstrap_admin(conn, "a@x.com", clock)
    b = users.invite(conn, actor=a, email="b@x.com", role="ADMIN", display_name="B", clock=clock)
    with pytest.raises(AuthError) as exc:
        local_admin.set_password(conn, a.user_id, "short")
    assert exc.value.code == "PASSWORD_TOO_SHORT"
    local_admin.set_password(conn, a.user_id, PASSWORD)
    local_admin.set_password(conn, b.user_id, PASSWORD)
    rows = conn.execute("select password_hash, salt from admin_credentials").fetchall()
    conn.close()
    # Same password, different salt, so different hash; the plaintext is never stored.
    assert rows[0]["salt"] != rows[1]["salt"] and rows[0]["password_hash"] != rows[1]["password_hash"]
    assert all(PASSWORD not in r["password_hash"] for r in rows)


def test_verify_raises_stable_codes(tmp_path: Path, clock: FakeClock) -> None:
    path, _ = _make(tmp_path, clock)
    conn = open_connection(path=path)
    assert local_admin.verify(conn, "boss@x.com", PASSWORD, clock).email == "boss@x.com"
    with pytest.raises(AuthError) as exc:
        local_admin.verify(conn, "boss@x.com", "nope nope nope nope", clock)
    assert exc.value.code == "BAD_CREDENTIALS"
    conn.close()


def _strip(r):  # type: ignore[no-untyped-def]
    # Status, content type and body without the per-request id.
    body = {k: v for k, v in r.json().items() if k != "request_id"}
    return r.status_code, r.headers["content-type"], body


def test_locked_admin_and_unknown_email_look_identical(tmp_path: Path, clock: FakeClock) -> None:
    _, client = _make(tmp_path, clock)
    for _ in range(5):
        _login(client, password="wrong password!!")
    locked = _login(client)
    unknown = _login(client, email="nobody@x.com")
    assert _strip(locked) == _strip(unknown)


def test_concurrent_wrong_guesses_cannot_beat_the_lock(tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    path, _ = _make(tmp_path, clock)
    # All five wrong guesses finish hashing (and so have read the counter) before any of them writes.
    barrier = threading.Barrier(5)
    real = local_admin._hash

    def slow(password: str, salt: bytes, *rest: int) -> str:
        out = real(password, salt, *rest)
        if password.startswith("wrong"):
            barrier.wait(timeout=20)
        return out

    monkeypatch.setattr(local_admin, "_hash", slow)
    codes: list[str] = []

    def guess() -> None:
        c = open_connection(path=path)
        try:
            local_admin.verify(c, "boss@x.com", "wrong password!!", clock)
        except AuthError as exc:
            codes.append(exc.code)
        finally:
            c.close()

    threads = [threading.Thread(target=guess) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    conn = open_connection(path=path)
    row = conn.execute("select failed_attempts, locked_until from admin_credentials c join users u using(user_id) "
                       "where u.email='boss@x.com'").fetchone()
    # Five total wrong guesses lock the account; none was lost to a race.
    assert row["failed_attempts"] == 5 and row["locked_until"] is not None
    with pytest.raises(AuthError) as exc:
        local_admin.verify(conn, "boss@x.com", PASSWORD, clock)
    assert exc.value.code == "LOCKED"
    conn.close()


def test_correct_password_cannot_succeed_after_lock_lands(tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    path, _ = _make(tmp_path, clock)
    # The correct attempt passes the lock check and hashes, then waits while the lock is written.
    hashed, proceed = threading.Event(), threading.Event()
    real = local_admin._hash

    def hooked(password: str, salt: bytes, *rest: int) -> str:
        out = real(password, salt, *rest)
        if password == PASSWORD:
            hashed.set()
            proceed.wait(timeout=20)
        return out

    monkeypatch.setattr(local_admin, "_hash", hooked)
    result: list[object] = []

    def right() -> None:
        c = open_connection(path=path)
        try:
            result.append(local_admin.verify(c, "boss@x.com", PASSWORD, clock))
        except AuthError as exc:
            result.append(exc.code)
        finally:
            c.close()

    t = threading.Thread(target=right)
    t.start()
    assert hashed.wait(timeout=20)
    conn = open_connection(path=path)
    for _ in range(5):
        with pytest.raises(AuthError):
            local_admin.verify(conn, "boss@x.com", "wrong password!!", clock)
    conn.close()
    proceed.set()
    t.join()
    assert result == ["LOCKED"]


def test_overlong_password_refused_without_hashing(tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    path, client = _make(tmp_path, clock)
    r = _login(client, password="x" * 1025)
    assert r.status_code == 401 and r.json()["reason_code"] == "BAD_CREDENTIALS"
    conn = open_connection(path=path)
    with pytest.raises(AuthError) as exc:
        local_admin.verify(conn, "boss@x.com", "x" * 1025, clock)
    assert exc.value.code == "BAD_CREDENTIALS"
    conn.close()


def test_hash_is_self_describing(tmp_path: Path, clock: FakeClock) -> None:
    path, _ = _make(tmp_path, clock)
    conn = open_connection(path=path, readonly=True)
    stored = conn.execute("select password_hash from admin_credentials").fetchone()[0]
    conn.close()
    assert re.fullmatch(r"scrypt\$\d+\$\d+\$\d+\$[0-9a-f]+\$[0-9a-f]+", stored)


def test_set_password_is_audited_without_secrets(tmp_path: Path, clock: FakeClock) -> None:
    path = make_working_db(tmp_path / "p.sqlite3")
    conn = open_connection(path=path)
    a = users.bootstrap_admin(conn, "a@x.com", clock)
    local_admin.set_password(conn, a.user_id, PASSWORD)
    rows = audit.query(conn, outcome="LOCAL_ADMIN_PASSWORD_SET")
    dump = " ".join(str(dict(r)) for r in conn.execute("select * from audit_log").fetchall())
    stored = conn.execute("select password_hash from admin_credentials").fetchone()[0]
    conn.close()
    assert len(rows) == 1 and rows[0]["target_id"] == a.user_id
    assert PASSWORD not in dump and stored not in dump


# ---- hardening sweep (11c group B) ----

@pytest.mark.parametrize("stored", [
    # An absurd cost factor: scrypt rejects it (ValueError) or cannot convert it (OverflowError).
    f"scrypt${2**200}$8$1${'ab' * 8}${'0' * 128}",
    f"scrypt${2**40}$8$1${'ab' * 8}${'0' * 128}",
    # A non-ASCII digest: compare_digest on str would raise TypeError.
    f"scrypt$16384$8$1${'ab' * 8}$h\u00e9llo",
])
def test_tampered_stored_hash_fails_closed(stored: str) -> None:
    assert local_admin._check("whatever password", stored) is False


def test_locked_path_runs_the_dummy_hash(tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    path, _ = _make(tmp_path, clock)
    conn = open_connection(path=path)
    for _ in range(5):
        with pytest.raises(AuthError):
            local_admin.verify(conn, "boss@x.com", "wrong password!!", clock)
    calls: list[bytes] = []
    real = local_admin._hash

    def spy(password: str, salt: bytes, *a: int) -> str:
        calls.append(salt)
        return real(password, salt, *a)

    monkeypatch.setattr(local_admin, "_hash", spy)
    # Locked: refused with LOCKED, and the dummy scrypt still ran, like the unknown-address path.
    with pytest.raises(AuthError) as e:
        local_admin.verify(conn, "boss@x.com", PASSWORD, clock)
    assert e.value.code == "LOCKED" and calls == [local_admin._DUMMY_SALT]
    conn.close()


@pytest.mark.parametrize("send", [
    {"content": b"not json", "headers": {"content-type": "application/json"}},
    {"json": {"email": "boss@x.com"}},
    {"json": ["boss@x.com", "pw"]},
    {"json": {"email": "boss@x.com", "password": "y" * 1025}},
])
def test_malformed_or_overlong_attempts_are_audited_without_their_values(tmp_path: Path, clock: FakeClock,
                                                                         send: dict[str, object]) -> None:
    path, client = _make(tmp_path, clock)
    r = client.post("/auth/local-admin", **send)  # type: ignore[arg-type]
    assert r.status_code == 401 and r.json()["reason_code"] == "BAD_CREDENTIALS"
    rows = [x for x in _rows(path) if x["outcome"] == "LOCAL_ADMIN_REFUSED"]
    # One refusal row, reason BAD_REQUEST, and none of the submitted values (not even the email).
    assert [(x["action"], x["outcome"], x["detail_json"]) for x in rows] == [("LOCAL_ADMIN", "LOCAL_ADMIN_REFUSED", '{"reason": "BAD_REQUEST"}')]
    assert rows[0]["actor_email"] is None and rows[0]["actor_user_id"] is None
    assert "boss@x.com" not in str(tuple(rows[0])) and "yyyy" not in str(tuple(rows[0]))
