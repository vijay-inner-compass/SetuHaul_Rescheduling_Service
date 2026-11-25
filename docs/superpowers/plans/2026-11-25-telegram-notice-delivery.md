# Telegram Notice Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The server sends queued driver notices to Telegram by itself.

**Architecture:** The existing one-pass sender gains an age limit and a small thread runner; the app starts it when the bot is configured, next to the existing maintenance sweep.

**Tech Stack:** Python 3.12, `threading`, SQLite, FastAPI lifespan, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-25-telegram-notice-delivery-design.md`

## Global Constraints
- Interval 5 seconds; age limit 30 minutes; an old notice becomes `PERMANENT_FAILURE`, `last_error='TOO_OLD'`.
- The bot token never reaches a log, audit row or error text.
- No real Telegram call in tests (`FakeTelegramClient`).
- Codebase style: lazy annotations, a comment on each logical step; `ruff` and `mypy src` clean.

## Review Focus
- A failing pass (database locked, unexpected client error) is logged and the next pass still runs (Task 1).
- A driver who links their chat late gets no backlog of old notices (Task 1).
- Shutting the server down stops the thread (Task 2).
- Without a configured bot nothing new starts (Task 2).

---

### Task 1: Age limit and runner

**Files:** Modify `src/setuhaul/jobs/send_driver_notifications.py`; Test `tests/test_telegram_delivery.py`.

**Interfaces:**
- Produces: `NOTICE_MAX_AGE = timedelta(minutes=30)`; `send_driver_notifications(conn, clock, client, limit=100, max_age=NOTICE_MAX_AGE) -> DeliveryResult` (too-old rows count in `permanent_failures`);
  `DriverNoticeRunner(clock, client, conn_factory, interval_s=5.0)` with `run_once() -> DeliveryResult | None` (None when the pass failed), `start()`, `stop()`.

- [ ] **Step 1:** Make the test helper stamp notices at `clock.now()`. Add failing tests: `test_a_notice_older_than_thirty_minutes_is_marked_and_not_sent` (31 min: nothing sent, `PERMANENT_FAILURE`/`TOO_OLD`; 29 min: sent); `test_a_late_binding_driver_gets_no_old_backlog`; `test_a_failing_pass_is_logged_without_secrets_and_the_next_one_sends` (client raises `RuntimeError("https://api.telegram.org/botSECRET/")` once: `run_once()` is None, log lacks `SECRET`, next `run_once()` sends); `test_the_runner_thread_sends_and_stops` (interval 0.01, wait on an Event set by the fake, `stop()`, thread not alive).
- [ ] **Step 2:** Run `pytest tests/test_telegram_delivery.py`. Expected: new tests FAIL.
- [ ] **Step 3:** Implement. The runner opens a connection per pass from `conn_factory` and closes it; it logs only the exception class name.
- [ ] **Step 4:** `pytest -q`. Expected: PASS.
- [ ] **Step 5:** Commit `feat: runner for queued driver notices, skipping old ones`.

### Task 2: Start it with the app, and say so

**Files:** Modify `src/setuhaul/api.py` (`AppDeps`, `lifespan`, `create_app_from_env`), `.env.example`, `docs/running-and-demonstrating.md`, `docs/decision-register.md`; Test `tests/test_telegram_delivery.py`.

**Interfaces:**
- Consumes: `DriverNoticeRunner`.
- Produces: `AppDeps.run_driver_notices: bool = False`, `AppDeps.notice_interval_s: float = 5.0`.

- [ ] **Step 1:** Failing tests: `test_the_app_delivers_while_it_runs_and_stops_on_shutdown` (`with TestClient(app)`, queued notice reaches the fake; after exit the thread is gone); `test_nothing_starts_without_a_bot`; `test_environment_with_a_bot_turns_on_delivery_and_the_sweep` (`create_app_from_env` with test values for `SETUHAUL_TELEGRAM_*`: both flags true; without them both false).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement: `run_driver_notices = tg_client is not None`; `run_maintenance = SETUHAUL_RUN_MAINTENANCE == "1" or tg_client is not None`. Update the documents (the "not delivered on their own" limitation, section 11, one-process limit, `.env.example`, including its outdated "BOTH of the next two" LangSmith line) and add decision D95.
- [ ] **Step 4:** `ruff check src tests && mypy src && pytest -q`. Expected: all PASS.
- [ ] **Step 5:** Commit `feat: the server sends queued driver notices when the bot is configured`.
