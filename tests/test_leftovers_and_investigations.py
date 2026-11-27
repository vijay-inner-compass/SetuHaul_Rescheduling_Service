"""Leftover fixes + deterministic V1-V4 investigation experiments."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import fakeredis
from fastapi.testclient import TestClient

from setuhaul import passes
from setuhaul.api import AppDeps, create_app
from setuhaul.auth import sessions, users
from setuhaul.clock import FakeClock
from setuhaul.conversation import (
    ConversationServices,
    InboundMessage,
    TrustedConversationContext,
    handle_message,
)
from setuhaul.db import open_connection
from setuhaul.escalation import JsonlEscalationSink, SqlEscalationSink
from setuhaul.identity import provision_identities
from setuhaul.jobs.expire_holds import MaintenanceRunner
from setuhaul.jobs.partner_mail import PartnerMailRunner
from setuhaul.jobs.send_driver_notifications import DriverNoticeRunner
from setuhaul.mail_transport import InMemoryMailbox
from setuhaul.memory import ConversationMemoryStore
from setuhaul.model_gateway import ModelPolicy, ModelResponse
from tests.helpers import seed_everything_known

SECRET = b"test-secret"
TG_SECRET = b"0123456789abcdef0123456789abcdef"


def _sign(raw: bytes) -> str:
    return hmac.new(SECRET, raw, hashlib.sha256).hexdigest()


# ---- Leftover 1: _unavailable must not say "Nothing has been booked or held" after a write committed --------

def test_unavailable_after_committed_hold_does_not_claim_nothing_held(
    working_db_path: Path, clock: FakeClock, monkeypatch: Any
) -> None:
    conn = open_connection(path=working_db_path)
    try:
        seed_everything_known(conn)
        provision_identities(conn)
        row = conn.execute(
            "select d.driver_id, d.phone, d.driver_name, s.shipment_id from drivers d "
            "join shipments s using(driver_id) where s.shipment_id='SHP1006'"
        ).fetchone()
        from setuhaul.identity import resolve_driver

        trusted = resolve_driver(conn, row["phone"])
        thread_id = "THR-UNAVAIL"
        conn.execute(
            "insert into chat_threads (thread_id, driver_id, shipment_id, opened_at, thread_status, thread_intent) "
            "values (?, ?, 'SHP1006', ?, 'OPEN', 'REPORT_DELAY')",
            (thread_id, trusted.driver_id, clock.now().isoformat()),
        )
        conn.commit()

        from setuhaul.stub_model import StubGateway

        # Simulate a database error AFTER the hold committed (when checking pass_check.prompt_key).
        def boom(*_a: object, **_kw: object) -> str | None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("setuhaul.conversation.pass_check.prompt_key", boom)

        mem = ConversationMemoryStore(conn, fakeredis.FakeRedis(decode_responses=True), clock)
        svc = ConversationServices(
            conn=conn,
            memory=mem,
            gateway=StubGateway(),
            policy=ModelPolicy(),
            sink=SqlEscalationSink(conn, clock),
            fallback_sink=JsonlEscalationSink(working_db_path.parent / "fb.jsonl"),
        )
        reply = handle_message(
            InboundMessage(message_id="m-hold-err", text="my truck is 60 minutes late"),
            TrustedConversationContext(driver=trusted, thread_id=thread_id),
            clock,
            svc,
        )
        # A real hold was committed! The reply must report the real hold and NOT falsely claim "Nothing has been booked or held".
        assert "Nothing has been booked or held" not in reply.text
        assert reply.verified_hold is True
    finally:
        conn.close()


# ---- Leftover 2: runners record last success and expose it to administrators --------------------------------

def test_runners_record_last_success_and_admin_endpoint_exposes_it(working_db_path: Path, clock: FakeClock) -> None:
    conn = open_connection(path=working_db_path)
    try:
        admin = users.bootstrap_admin(conn, "admin@x.com", clock)
        assert admin is not None
        token = sessions.create(conn, admin, "GOOGLE", clock)
        conn.commit()

        MaintenanceRunner(conn, clock).run_once()
        box = InMemoryMailbox()
        PartnerMailRunner(conn, clock, box, box).run_once()
    finally:
        conn.close()

    class FakeTg:
        def send(self, chat_id: int, text: str, request_contact_button: bool = False) -> None:
            return None

    DriverNoticeRunner(clock, FakeTg(), lambda: open_connection(path=working_db_path)).run_once()

    deps = AppDeps(
        db_path=working_db_path,
        redis=fakeredis.FakeRedis(decode_responses=True),
        webhook_secret=SECRET,
        coordinator_tokens={},
        fallback_path=working_db_path.parent / "fb.jsonl",
        clock=clock,
    )
    client = TestClient(create_app(deps))
    client.cookies.set(sessions.COOKIE_NAME, token)
    r = client.get("/v1/admin/runners")
    assert r.status_code == 200
    names = {row["job_name"] for row in r.json()["runners"]}
    assert {"maintenance_runner", "driver_notice_runner", "partner_mail_runner"}.issubset(names)


# ---- Leftover 3: Telegram failed reply queued even when driver has no active load ---------------------------

def test_telegram_failed_reply_is_queued_even_without_active_load(working_db_path: Path, clock: FakeClock) -> None:
    conn = open_connection(path=working_db_path)
    try:
        drv = conn.execute("select driver_id from drivers order by driver_id limit 1").fetchone()[0]
        # Cancel all shipments for this driver so active_shipment_for_driver returns None.
        conn.execute("update shipments set current_status='COMPLETED' where driver_id=?", (drv,))
        conn.execute(
            "insert into telegram_identities (driver_id, telegram_user_id, chat_id, bound_at) values (?, '777', '777', ?)",
            (drv, clock.now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    from setuhaul.auth.telegram import TelegramSendError

    class FailingTg:
        def send(self, chat_id: int, text: str, request_contact_button: bool = False) -> None:
            raise TelegramSendError("network down")

    deps = AppDeps(
        db_path=working_db_path,
        redis=fakeredis.FakeRedis(decode_responses=True),
        webhook_secret=SECRET,
        coordinator_tokens={},
        fallback_path=working_db_path.parent / "fb.jsonl",
        clock=clock,
        telegram_secret=TG_SECRET,
        telegram=FailingTg(),
    )
    client = TestClient(create_app(deps))
    # Send an unsupported-content update (photo, no text) which calls reply(...) directly.
    update = {
        "update_id": 9001,
        "message": {"chat": {"id": 777, "type": "private"}, "from": {"id": 777, "is_bot": False}, "photo": [{}]},
    }
    r = client.post(
        "/v1/telegram/webhook",
        json=update,
        headers={"x-telegram-bot-api-secret-token": TG_SECRET.decode()},
    )
    assert r.status_code == 200

    check = open_connection(path=working_db_path, readonly=True)
    try:
        queued = check.execute(
            "select count(*) from notification_outbox where recipient_type='DRIVER' and recipient_ref=?",
            (drv,),
        ).fetchone()[0]
        assert queued == 1
    finally:
        check.close()


# ---- V1: two quick messages from one driver overwriting conversation state ----------------------------------

def test_v1_concurrent_driver_messages_do_not_overwrite_state(working_db_path: Path, clock: FakeClock) -> None:
    conn = open_connection(path=working_db_path)
    try:
        provision_identities(conn)
        phone = conn.execute(
            "select d.phone from drivers d join shipments s using(driver_id) where s.shipment_id='SHP1006'"
        ).fetchone()[0]
    finally:
        conn.close()

    started = threading.Event()
    release_first = threading.Event()
    call_order: list[str] = []
    lock = threading.Lock()

    class OverlappingGateway:
        def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], policy: ModelPolicy) -> ModelResponse:
            user_text = next((m.content for m in reversed(messages) if m.role == "user"), "")
            with lock:
                call_order.append(str(user_text))
                is_first = len(call_order) == 1
            if is_first:
                started.set()
                release_first.wait(timeout=2.0)
            return ModelResponse(text=f"ok:{user_text}", tool_calls=())

    deps = AppDeps(
        db_path=working_db_path,
        redis=fakeredis.FakeRedis(decode_responses=True),
        webhook_secret=SECRET,
        coordinator_tokens={},
        fallback_path=working_db_path.parent / "fb.jsonl",
        clock=clock,
        gateway_factory=OverlappingGateway,
    )
    client = TestClient(create_app(deps))
    results: dict[str, int] = {}

    def send_msg(mid: str, text: str) -> None:
        raw = json.dumps({"from": phone, "message_id": mid, "text": text}).encode()
        r = client.post("/v1/driver/messages", content=raw, headers={"x-signature": _sign(raw)})
        results[mid] = r.status_code

    t1 = threading.Thread(target=send_msg, args=("m1", "first message"))
    t1.start()
    assert started.wait(timeout=2.0)
    t2 = threading.Thread(target=send_msg, args=("m2", "second message"))
    t2.start()
    time.sleep(0.15)
    # Check whether m2 entered the model WHILE m1 was still running (concurrent overlap = V1 reproduced!).
    with lock:
        overlapped = len(call_order) >= 2
    release_first.set()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)
    # After fix: per-driver serialisation prevents m2 from overlapping m1!
    assert not overlapped, "V1 reproduced: second message entered model while first was still running"
    assert results == {"m1": 200, "m2": 200}


# ---- V2: Telegram retry acknowledged with 200 while first attempt is still running --------------------------

def test_v2_telegram_retry_while_first_attempt_in_progress_does_not_return_200(
    working_db_path: Path, clock: FakeClock
) -> None:
    conn = open_connection(path=working_db_path)
    try:
        provision_identities(conn)
        drv = conn.execute("select driver_id from shipments where shipment_id='SHP1006'").fetchone()[0]
        conn.execute(
            "insert into telegram_identities (driver_id, telegram_user_id, chat_id, bound_at) values (?, '888', '888', ?)",
            (drv, clock.now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    started = threading.Event()
    release = threading.Event()

    class SlowGateway:
        def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], policy: ModelPolicy) -> ModelResponse:
            started.set()
            release.wait(timeout=2.0)
            return ModelResponse(text="done", tool_calls=())

    class FakeTg:
        def send(self, chat_id: int, text: str, request_contact_button: bool = False) -> None:
            return None

    deps = AppDeps(
        db_path=working_db_path,
        redis=fakeredis.FakeRedis(decode_responses=True),
        webhook_secret=SECRET,
        coordinator_tokens={},
        fallback_path=working_db_path.parent / "fb.jsonl",
        clock=clock,
        telegram_secret=TG_SECRET,
        telegram=FakeTg(),
        gateway_factory=SlowGateway,
    )
    client = TestClient(create_app(deps))
    update = {
        "update_id": 4242,
        "message": {"chat": {"id": 888, "type": "private"}, "from": {"id": 888, "is_bot": False}, "text": "hello"},
    }
    headers = {"x-telegram-bot-api-secret-token": TG_SECRET.decode()}
    t1 = threading.Thread(target=lambda: client.post("/v1/telegram/webhook", json=update, headers=headers))
    t1.start()
    assert started.wait(timeout=2.0)
    # Telegram retries the same update_id while attempt 1 is still running:
    r_retry = client.post("/v1/telegram/webhook", json=update, headers=headers)
    release.set()
    t1.join(timeout=5.0)
    # Must NOT return 200 while attempt 1 is still in progress (or Telegram stops retrying if attempt 1 fails)!
    assert r_retry.status_code == 503


# ---- V4: two paths sending a replacement pass at the same time ----------------------------------------------

def test_v4_concurrent_replacement_pass_sends_only_one_pass(working_db_path: Path, clock: FakeClock) -> None:
    conn = open_connection(path=working_db_path)
    try:
        drv = conn.execute("select driver_id from shipments where shipment_id='SHP1006'").fetchone()[0]
        conn.execute(
            "insert into telegram_identities (driver_id, telegram_user_id, chat_id, bound_at) values (?, '999', '999', ?)",
            (drv, clock.now().isoformat()),
        )
        admin = users.bootstrap_admin(conn, "admin@x.com", clock)
        assert admin is not None
        conn.execute(
            "insert or ignore into shipment_assignments (shipment_id, created_by, created_at) values ('SHP1006', ?, ?)",
            (admin.user_id, clock.now().isoformat()),
        )
        # Issue an initial pass and revoke it with WAREHOUSE_CHANGED so send_pending wants to replace it.
        passes.issue(conn, "SHP1006", clock, None)
        passes.move_warehouse(conn, "SHP1006", clock)
        conn.commit()
    finally:
        conn.close()

    sent_codes: list[str] = []
    send_lock = threading.Lock()
    entered = threading.Event()
    release = threading.Event()

    class SlowTg:
        def send(self, chat_id: int, text: str, request_contact_button: bool = False) -> None:
            with send_lock:
                sent_codes.append(text)
                first = len(sent_codes) == 1
            if first:
                entered.set()
                release.wait(timeout=1.5)

    tg = SlowTg()

    def run_pending() -> None:
        c = open_connection(path=working_db_path)
        try:
            passes.send_pending(c, tg, clock)
        finally:
            c.close()

    def run_waiting() -> None:
        c = open_connection(path=working_db_path)
        try:
            passes.send_waiting(c, tg, drv, clock)
        finally:
            c.close()

    t1 = threading.Thread(target=run_pending)
    t2 = threading.Thread(target=run_waiting)
    t1.start()
    assert entered.wait(timeout=2.0)
    t2.start()
    time.sleep(0.15)
    release.set()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    # After fix: only ONE replacement pass is issued and sent!
    assert len(sent_codes) == 1, f"V4 reproduced: {len(sent_codes)} replacement passes sent concurrently"


# ---- V3: slow model calls saturating worker threads and starving /ready -------------------------------------

def test_v3_slow_model_calls_do_not_starve_ready_check(working_db_path: Path, clock: FakeClock) -> None:
    import anyio
    import httpx

    conn = open_connection(path=working_db_path)
    try:
        provision_identities(conn)
        phones = [
            str(r[0])
            for r in conn.execute("select phone from drivers where driver_status='ACTIVE' order by driver_id limit 5").fetchall()
        ]
    finally:
        conn.close()

    release = threading.Event()
    in_model = threading.Event()
    active_calls = 0
    lock = threading.Lock()

    class HangingGateway:
        def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], policy: ModelPolicy) -> ModelResponse:
            nonlocal active_calls
            with lock:
                active_calls += 1
                if active_calls >= 4:
                    in_model.set()
            release.wait(timeout=3.0)
            return ModelResponse(text="slow", tool_calls=())

    deps = AppDeps(
        db_path=working_db_path,
        redis=fakeredis.FakeRedis(decode_responses=True),
        webhook_secret=SECRET,
        coordinator_tokens={},
        fallback_path=working_db_path.parent / "fb.jsonl",
        clock=clock,
        gateway_factory=HangingGateway,
    )
    app = create_app(deps)

    async def scenario() -> tuple[float, int]:
        # Shrink AnyIO's default thread limiter to 4 to reproduce 40-thread saturation deterministically in a unit test.
        limiter = anyio.to_thread.current_default_thread_limiter()
        old = limiter.total_tokens
        limiter.total_tokens = 4
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                async def slow_msg(i: int) -> None:
                    raw = json.dumps({"from": phones[i], "message_id": f"v3-{i}", "text": "hi"}).encode()
                    await ac.post("/v1/driver/messages", content=raw, headers={"x-signature": _sign(raw)})

                tasks = [anyio.create_task_group() for _ in range(1)]
                async with tasks[0] as tg:
                    for i in range(4):
                        tg.start_soon(slow_msg, i)
                    # Wait until all 4 worker threads are busy inside the slow model call.
                    deadline = time.monotonic() + 2.0
                    while not in_model.is_set() and time.monotonic() < deadline:
                        await anyio.sleep(0.02)
                    t0 = time.perf_counter()
                    r_ready = await ac.get("/ready")
                    elapsed = time.perf_counter() - t0
                    release.set()
                return elapsed, r_ready.status_code
        finally:
            release.set()
            limiter.total_tokens = old

    elapsed_s, status = anyio.run(scenario)
    assert status == 200
    # If /ready ran on the saturated default threadpool, it would block for ~3.0s until release!
    assert elapsed_s < 0.5, f"V3 reproduced: /ready blocked for {elapsed_s:.2f}s while worker threads were busy in model calls"


# ---- F17: gate pass scanning and pinned container base images -----------------------------------------------

def test_gate_page_includes_pass_scanner_controls(working_db_path: Path, clock: FakeClock) -> None:
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
        webhook_secret=SECRET,
        coordinator_tokens={},
        fallback_path=working_db_path.parent / "fb.jsonl",
        clock=clock,
    )
    client = TestClient(create_app(deps))
    client.cookies.set(sessions.COOKIE_NAME, token)
    r = client.get("/gate")
    assert r.status_code == 200
    assert 'id="scan-pass"' in r.text and 'id="scan-video"' in r.text and "autofocus" in r.text


def test_container_base_images_are_pinned_by_digest() -> None:
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert dockerfile.count("@sha256:") >= 2
    assert "redis:7-alpine@sha256:" in compose
