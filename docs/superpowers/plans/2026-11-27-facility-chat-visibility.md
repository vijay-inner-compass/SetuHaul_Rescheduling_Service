# Facility Chat Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Warehouse authorities see exactly the chat messages about loads going to their facilities.

**Architecture:** An extension table records load and facility per message. One module writes it; the chat query reads it.

**Tech Stack:** Python 3.12, SQLite, pytest.

**Spec:** `docs/superpowers/specs/2026-11-27-facility-chat-visibility-design.md`

## Global Constraints

- Branch `feat/facility-chats` from `main`. Do not push.
- `chat_messages` and `chat_threads` are not altered; no supplied row is changed.
- Recording a load must never make a driver's message or reply fail.
- A warehouse authority with no facility sees no chats. Admin behaviour is unchanged.
- Finish with ruff, mypy and `pytest -q`.

## Review Focus

1. A driver with two unfinished loads and nothing selected: recorded for neither, admin only (Task 1 test).
2. A facility change after the message: the old warehouse still sees it, the new one does not (Task 1 test).
3. A duplicate delivery of one Telegram update: no second record, no error (Task 2 test).
4. Another warehouse's authority never receives a message through the API's `limit`/`messages` parameters (Task 3 test).
5. A saved load that was deleted/cancelled meanwhile: the message keeps its record (Task 1 test).

## Tasks

### Task 1: The record

**Files:** Create `migrations/021_chat_message_loads.sql`, `src/setuhaul/chat_loads.py`, `tests/test_chat_loads.py`.

**Interfaces (produces):**
- Table `chat_message_loads(chat_message_id TEXT PRIMARY KEY REFERENCES chat_messages, shipment_id TEXT NOT NULL
  REFERENCES shipments, facility_id TEXT NOT NULL REFERENCES facilities)`, index on `facility_id`. The migration records
  existing messages whose thread has a `shipment_id`, with that shipment's destination.
- `chat_loads.mark(conn) -> int`: the highest `chat_messages.rowid` now.
- `chat_loads.record(conn, thread_id: str, after: int, shipment_id: str | None) -> None`: records the thread's messages
  written after `after` and not yet recorded; does nothing when `shipment_id` is None. Caller's transaction.
- `chat_loads.load_of_turn(conn, driver_id: str, selected: str | None) -> str | None`: `selected` if it is the driver's
  unfinished load, else their only unfinished load, else None.

- [ ] Tests: `test_record_covers_only_new_messages`, `test_two_loads_and_nothing_selected_records_nothing`,
  `test_selected_load_wins_while_unfinished`, `test_facility_is_fixed_when_written`, `test_migration_records_named_threads`.
- [ ] Run (FAIL), implement, run with `tests/test_audit.py tests/test_deploy_start.py` (PASS), commit
  `feat: record which load a chat message is about`.

### Task 2: Record at every writer

**Files:** Modify `api.py` (pass-code reply near line 580 and the pipeline turn), `conversation.py`, `facility_change.py:150`
(`_tell`), `escalation.py` (`_post_update`), `partner_booking.py:170`, `position.py` (appends at 123, 128, 181); test
`tests/test_chat_loads.py`.

**Interfaces:** consumes Task 1. Each system writer passes its own shipment. The driver turn takes `mark` before the first
append and calls `record` once after the reply with `load_of_turn(selected_shipment_id)`. Failures are logged and swallowed.

- [ ] Tests, each through the real path: `test_driver_turn_is_recorded`, `test_duplicate_update_records_once`,
  `test_warehouse_notice_is_recorded` (free a slot), `test_case_message_is_recorded`, `test_partner_answer_is_recorded`,
  `test_offer_from_position_is_recorded`, `test_recording_failure_does_not_fail_the_turn`.
- [ ] Run (FAIL), implement, run with `tests/test_conversation.py tests/test_telegram_channel.py tests/test_exception_routes.py`
  (PASS), commit `feat: tag chat messages with their load`.

### Task 3: Who sees what

**Files:** Modify `auth/warehouse_routes.py` (`_windowed_chats` becomes `_facility_chats`), `auth/pages.py` (note),
`auth/projects.py` (remove `chat_window_starts` if unused), `auth/wiki_pages.py`, `docs/running-and-demonstrating.md`;
tests `tests/test_driver_chat_window.py` (rewritten for the new rule), `tests/test_auth_role_matrix.py`,
`tests/test_auth_pages.py`.

**Interfaces:** `driver_chats_in_scope` keeps its signature and output shape. For a non-admin: messages joined to
`chat_message_loads` filtered by `facility_clause(user, "l.facility_id")`, newest drivers first, capped as today.

- [ ] Tests: `test_authority_sees_messages_for_their_facility_only`, `test_driver_they_did_not_add_is_visible`,
  `test_unrecorded_messages_are_admin_only`, `test_no_facility_sees_nothing`, `test_limits_never_leak`.
- [ ] Run (FAIL), implement, run (PASS); then ruff, mypy, `pytest -q`; commit
  `feat: show warehouse users the chats of their facility's loads`.
