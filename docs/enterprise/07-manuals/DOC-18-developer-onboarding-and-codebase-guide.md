# DOC-18: Developer Onboarding Guide & Codebase Navigation Guide

| Field | Value |
|---|---|
| **Document ID** | `DOC-18` |
| **Version** | `1.0.0` |
| **Category** | 7 — Guides & Manuals |
| **Repository Commit** | `445c7f965e22e7915238cca5631e68b4462d7b7b` |
| **Validation Status** | Verified against repository structure and tooling |

---

## 1. Purpose and Scope
This guide enables a new engineer to set up a local development environment, understand architectural invariants, navigate the ~108 Python source files and 142 test files, and safely contribute without violating core domain or security contracts.

## 2. Executive Summary
SetuHaul is a clean, strictly typed (`mypy --strict`), heavily commented Python 3.12 codebase with zero frontend build tools and fast local verification (`[Verified]`). New contributors must internalize three golden rules: **(1) never mutate `data/setuhaul.sqlite3`**, **(2) never let the LLM own booking state or identity**, and **(3) never perform network I/O inside `with immediate(conn):` write transactions** (`[Verified]`).

---

## 3. First-Day Developer Setup `[Verified]`
1. **Install Python 3.12** and create virtualenv:
   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\python -m pip install -e ".[dev]"
   ```
2. **Verify quality gates**:
   ```powershell
   .\.venv\Scripts\python -m ruff check .
   .\.venv\Scripts\python -m mypy src
   .\.venv\Scripts\python -m pytest -q    # 2,127 tests
   ```
3. **Launch local demo console** (localhost-only, simulated clock, in-memory partner mailbox):
   ```powershell
   .\.venv\Scripts\python scripts\run_demo.py --auto-login
   ```
   Open `http://127.0.0.1:8800/` (auto-logged into `/warehouse` as `demo@localhost.test`; `/demo` for demo console; `/demo/driver-login?driver=DRV006` for demo driver chat).
4. **Recommended reading order**:
   - `docs/enterprise/03-architecture/DOC-07-software-architecture-and-hld.md`
   - `docs/code-walkthrough/README.md` (existing 8-part layer walkthrough)
   - `docs/enterprise/03-architecture/DOC-10-data-architecture.md`

---

## 4. Codebase Navigation Guide `[Verified]`

| Directory / File Group | Purpose | Key Entry Points |
|---|---|---|
| `src/setuhaul/config.py`, `clock.py`, `db.py`, `schemas.py` | Foundations: frozen `Settings`, injectable `Clock`, SQLite WAL + `immediate()`, IST `Aware` types. | `load_settings()`, `open_connection()`, `immediate()`, `apply_migrations()` |
| `src/setuhaul/eta.py`, `gps.py`, `travel.py`, `position.py` | ETA derivation, Haversine road estimation, 15m GPS freshness & arrival floor, proactive re-offers. | `derive_eta()`, `gps_arrival_floor()`, `handle_driver_position()` |
| `src/setuhaul/capacity.py`, `feasibility.py`, `allocation.py`, `option_history.py` | Facility snapshots, 5-gate slot evaluation, FCFS ranking, option reconciliation. | `build_facility_state()`, `evaluate_slot()`, `search_feasible_slots()`, `diff_option_set()` |
| `src/setuhaul/booking.py`, `cancellation.py`, `release.py`, `assign.py` | Holds (`APT-H-...`), atomic confirm swap, two-turn cancel, warehouse slot release & case slot assign. | `acquire_hold()`, `confirm_reschedule()`, `cancel_confirmed_appointment()`, `free_slot()`, `assign_in_txn()` |
| `src/setuhaul/round_clock.py`, `dock_blocks.py`, `board.py` | 24/7 14d slot/labour top-up, dock block preview/commit/reopen, dock board view. | `top_up()`, `preview()`, `block()`, `build_board()` |
| `src/setuhaul/facility_change.py`, `partner_booking.py`, `mail_transport.py`, `mailcfg.py` | Facility change approval/revert; SMTP/IMAP partner warehouse booking. | `request_change()`, `decide()`, `after_approval()`, `settle()`, `handle_reply()` |
| `src/setuhaul/dock_pass.py`, `passes.py`, `pass_check.py`, `gate.py`, `order_numbers.py` | 128-bit `DAP-...` passes, chat confirmation lockout, gate check-in/out & overstay sweep. | `issue()`, `pass_check.check()`, `validate()`, `check_in()`, `check_out()`, `sweep()` |
| `src/setuhaul/conversation.py`, `model_gateway.py`, `prompts.py`, `tools.py`, `language.py`, `memory.py` | Conversational loop, LangGraph bounded runner, 14 tools, trilingual templates, output guards. | `handle_message()`, `run_bounded_loop()`, `ToolRegistry.execute()` |
| `src/setuhaul/escalation.py`, `coordinator.py`, `events.py`, `notifications.py`, `driver_messages.py`, `chat_loads.py` | Escalation queue, CLI, JSONL fallback, outbox, driver messaging, load-scoped chat tags. | `create_handoff()`, `ingest_fallback_file()`, `enqueue_downstream()` |
| `src/setuhaul/api.py` | FastAPI app factory, lifespan runners, middleware, driver/GPS/coordinator endpoints. | `create_app()`, `create_app_from_env()` |
| `src/setuhaul/auth/` | Web UI SSR pages (`*_pages.py`), JSON routes (`*_routes.py`, `admin_api.py`), AuthN/AuthZ, Telegram, assets (`assets/*.js,*.css`). | `require_role()`, `facility_clause()`, `same_origin_only()`, `audit.record()` |
| `src/setuhaul/jobs/` | Background runners (`expire_holds.py`, `send_driver_notifications.py`, `partner_mail.py`, etc.). | `MaintenanceRunner`, `DriverNoticeRunner`, `PartnerMailRunner` |
| `src/setuhaul/app_settings.py`, `runtime_settings.py`, `retention.py`, `observability.py`, `evaluation.py`, `integrity.py` | Fernet settings, retention/erasure, redacted JSON logging, trace replay, 12 DB invariants. | `check_invariants()`, `replay_trace()`, `erase_driver_data()` |
| `migrations/` | 27 sequential SQL migrations (`001`–`027`). | Always add `028_*.sql`; never edit applied migrations. |
| `tests/` & `scripts/` | 142 test modules (`2,127` tests), 9 traces (`tests/traces/`), verification & demo scripts. | `scripts/run_scenario.py`, `scripts/replay_traces.py`, `scripts/run_integrity_proof.py` |

---

## 5. Engineering Conventions & Non-Negotiable Rules `[Verified]`
1. **Strict Typing & Formatting**: `ruff` line-length `100`, target `py312`; `mypy --strict` must pass on `src/`.
2. **Injectable Time**: Always accept `clock: Clock`; never call `datetime.now()` in domain services.
3. **Migrations**: Never modify `data/setuhaul.sqlite3` or existing migrations; add new numbered `.sql` files and ensure `PRAGMA foreign_key_check` passes.
4. **No `innerHTML` in Production UI**: All JS in `src/setuhaul/auth/assets/` must use `textContent` / DOM APIs and preserve strict CSP (`style-src 'self'`, `script-src 'self'`).
5. **Test Discipline**: Any change to scheduling/holds/passes must pass `integrity.check_invariants()` and `scripts/replay_traces.py`.

## 6. Source Evidence & Open Questions
- Evidence: `pyproject.toml`, `README.md`, `src/setuhaul/`, `tests/`, `scripts/`.
- Open Questions: None.
