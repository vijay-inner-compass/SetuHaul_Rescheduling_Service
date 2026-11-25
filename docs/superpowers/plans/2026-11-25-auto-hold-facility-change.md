# Auto-hold and Facility Change Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Offer a driver one held slot at a time, and let a driver ask to move a load to another facility, which a warehouse user approves.

**Architecture:** The existing search, hold and confirm paths stay; Python holds the first ranked option right after a search, and a new "decline" tool releases it and holds the next. Facility change is a new request table and service; approval updates the load's destination and then reuses the same auto-hold (or the existing partner email request).

**Tech Stack:** Python 3.12, FastAPI, SQLite migrations, existing pydantic tools and language templates; pytest with the scripted stub model. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-25-auto-hold-facility-change-design.md`

## Global Constraints

- Nothing is booked without the driver's confirmation in a later message (existing `_can_confirm` rule stays).
- Python decides which slot is held; the model never picks or confirms on its own. Driver-visible booking facts are worded from stored rows, never model text.
- Every slot message names the facility, dock, date, time, the hold's expiry and what to reply, in `en`, `hi` and `hinglish`.
- Only `shipments.destination_facility_id` may change, only for an approved request; the original is kept in `facility_change_requests`. No other supplied column is updated.
- The old appointment ends only in the same transaction that confirms the new one.
- Only a warehouse authority bound to the **destination** facility, or an admin, approves or rejects. Drivers never see the dock board.
- Partner destination: the existing partner email request is used; the driver is never told it is booked before the partner approves.
- Every new route is classified in `tests/test_auth_role_matrix.py`; facility scoping uses `authz.facility_clause` / `assert_facility`.
- Tests run with the repository venv; `tests/conftest.py` already keeps them away from real services. Codebase style: lazy annotations, a comment on each logical step; `ruff` and `mypy` clean.

## Review Focus

- "No problem, yes" or "no, that is fine" must not be read as declining the held slot (Task 2).
- The hold expires before the driver replies "Yes": say so and offer again, never confirm (Task 2).
- A second change request, or approval of one already decided, changes nothing (Task 4).
- A load already in the dock or completed cannot be moved, including if it gets there between request and approval (Task 4).
- No slot at the new facility after approval: the original booking and a clear message survive, and a person is asked (Task 6).

---

### Task 1: Hold the first option automatically

**Files:** Modify `src/setuhaul/tools.py` (`_search_slot_options`, `_offer_original_slot`, `TurnState`), `src/setuhaul/language.py`, `src/setuhaul/conversation.py` (`_finalize`), `src/setuhaul/prompts.py`, `src/setuhaul/stub_model.py`; Test `tests/test_auto_hold.py`; update tests and traces that expect a numbered menu.

**Interfaces:**
- Produces: `tools._hold_rank(ctx: ToolContext, option_set_id: str, rank: int) -> dict[str, Any]` (shared by search, decline and `_request_hold`; returns the existing hold facts plus `facility`); `_slot_facts` gains key `facility`; template `hold_offer` with fields `facility, dock, date, start, end, until`.

- [ ] **Step 1:** Write failing tests: `test_delay_report_holds_the_first_option` (after "I will be two hours late": one HELD appointment for the shipment, the reply names the facility and dock, contains no "1." / "2." menu, and nothing is confirmed); `test_hold_message_in_hindi_and_hinglish_names_the_facility`; `test_yes_in_the_next_message_confirms`; `test_no_hold_possible_moves_to_the_next_option` (slot taken between search and hold: rank 2 is held).
- [ ] **Step 2:** Run `pytest tests/test_auto_hold.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement: after `record_option_set`, call `_hold_rank` for ranks in order until one returns HELD; set `ctx.turn.hold` and do not set `options_shown`. Keep recording the option set (decline needs it). Update the system prompt: a slot is already held after a search; ask for yes or no.
- [ ] **Step 4:** Run the new tests, then `pytest -q`; update assertions that expected a menu, without weakening safety assertions. Expected: PASS.
- [ ] **Step 5:** Commit `feat: hold the first feasible slot and ask the driver to confirm`.

### Task 2: "No" holds the next slot

**Files:** Modify `src/setuhaul/tools.py` (new tool), `src/setuhaul/schemas.py` if the tool list is typed there, `src/setuhaul/memory.py` (state field), `src/setuhaul/stub_model.py`, `src/setuhaul/language.py`; Test `tests/test_auto_hold.py`.

**Interfaces:**
- Consumes: `_hold_rank`.
- Produces: tool `decline_held_slot(driver_quote: str)`, available only when `_can_confirm(ctx)` is true; conversation state field `offered_slot_ids: tuple[str, ...]`.

- [ ] **Step 1:** Failing tests: `test_no_releases_and_holds_the_next`; `test_after_the_third_option_a_new_search_skips_offered_slots`; `test_nothing_left_hands_off_and_keeps_the_original_booking` (case reason `NO_SLOT`, original appointment still current, no live hold); `test_a_yes_containing_the_word_no_confirms` (`"no problem, yes confirm"` confirms); `test_yes_after_expiry_does_not_confirm`.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement: release via `release_hold(..., "DRIVER_CANCELLED")`, add the slot to `offered_slot_ids`, hold the next rank not offered; when the set is used up run the search again filtering offered slots; if none, `_handoff(ctx, "NO_SLOT", ...)`. The quote must be present in the driver's message (`_quote_present`).
- [ ] **Step 4:** `pytest -q`. Expected: PASS.
- [ ] **Step 5:** Commit `feat: declining a held slot offers the next one`.

### Task 3: Name the facility in every booking message

**Files:** Modify `src/setuhaul/language.py`, `src/setuhaul/booking.py` (`confirmed_appointment_facts` adds `facility`), `src/setuhaul/notifications.py`; Test `tests/test_auto_hold.py`.

- [ ] **Step 1:** Failing test `test_confirmed_released_and_status_replies_name_the_facility` for all three languages.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Add `{facility}` to `confirmed`, `hold_released` and the status wording; pass it from stored rows.
- [ ] **Step 4:** `pytest -q`. Expected: PASS.
- [ ] **Step 5:** Commit `feat: booking messages say which facility`.

### Task 4: Facility change requests

**Files:** Create `migrations/013_facility_change.sql`, `src/setuhaul/facility_change.py`; Modify `docs/writable-data-contract.md`, `docs/decision-register.md`; Test `tests/test_facility_change.py`.

**Interfaces:**
- Produces: table `facility_change_requests(request_id, shipment_id, driver_id, from_facility_id, to_facility_id, status CHECK PENDING|APPROVED|REJECTED|EXPIRED, decided_by, created_at, decided_at, expires_at)` with a partial unique index on `shipment_id` where status is PENDING;
  `request_change(conn, clock, *, driver_id: str, shipment_id: str, to_facility_id: str) -> ChangeResult` (status `REQUESTED|ALREADY_PENDING|SAME_FACILITY|UNKNOWN_FACILITY|NOT_MOVABLE`);
  `decide(conn, clock, *, actor: User, request_id: str, approve: bool) -> ChangeResult` (`APPROVED|REJECTED|NOT_PENDING|NOT_MOVABLE|NOT_FOUND`);
  `expire_due(conn, clock) -> int`. Request life: 120 minutes.

- [ ] **Step 1:** Failing tests: `test_one_open_request_per_shipment`; `test_foreign_shipment_refused`; `test_in_dock_or_completed_cannot_move`; `test_load_reaching_dock_before_approval_is_refused`; `test_only_destination_warehouse_or_admin_decides` (others get NOT_FOUND); `test_approve_changes_only_destination_facility_id` (every other column of the row is unchanged); `test_reject_changes_nothing`; `test_deciding_twice_is_not_pending`; `test_expired_request_cannot_be_approved`; `test_decisions_are_audited`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement each operation inside one `immediate()` transaction; audit actions `FACILITY_CHANGE_REQUESTED|APPROVED|REJECTED`. Movable statuses are the same set `partner_booking` treats as reschedulable.
- [ ] **Step 4:** `pytest -q`. Expected: PASS.
- [ ] **Step 5:** Commit `feat: facility change requests with destination approval`.

### Task 5: The driver asks in chat

**Files:** Modify `src/setuhaul/tools.py`, `src/setuhaul/stub_model.py`, `src/setuhaul/language.py`, `src/setuhaul/conversation.py`; Test `tests/test_facility_change_chat.py`.

**Interfaces:**
- Consumes: `facility_change.request_change`.
- Produces: tool `request_facility_change(shipment_id: str, facility_id: str, driver_quote: str)`; templates `change_requested`, `change_refused`; `TurnState.facility_change`.

- [ ] **Step 1:** Failing tests: `test_change_to_gurugram_creates_a_pending_request`; `test_reply_says_it_is_only_a_request_and_names_both_facilities`; `test_booking_is_untouched_while_pending`; `test_second_ask_reports_the_pending_one`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement; the facility list offered to the model is the active facilities other than the load's own. Replies come from the stored request.
- [ ] **Step 4:** `pytest -q`. Expected: PASS.
- [ ] **Step 5:** Commit `feat: drivers can ask to change facility`.

### Task 6: Approval screen and what happens next

**Files:** Create `src/setuhaul/auth/facility_routes.py`; Modify `src/setuhaul/auth/pages.py` (list, attention strip), `src/setuhaul/auth/assets/warehouse.js` (live list), `src/setuhaul/demo_driver.py`, `src/setuhaul/demo/driver.html` (pending request on the card), `src/setuhaul/facility_change.py` (follow-up), `tests/test_auth_role_matrix.py`; Test `tests/test_facility_change_surface.py`.

**Interfaces:**
- Consumes: `facility_change.decide`, `acquire_hold`, `partner_booking.request_slot`.
- Produces: `GET /v1/warehouse/facility-changes`, `POST /v1/warehouse/facility-changes/{request_id}/approve|reject`; `facility_change.after_approval(conn, clock, request_id) -> str` (`HELD|PARTNER_REQUESTED|NO_SLOT`), which writes the driver's message into their thread and queues the notice.

- [ ] **Step 1:** First verify in a test that `confirm_reschedule` can replace an appointment at one facility with a slot at another (`test_confirm_across_facilities_ends_the_old_one_atomically`). If it cannot, stop and report: that is a change to the spec.
- [ ] **Step 2:** Failing tests: `test_approval_holds_a_slot_at_the_new_facility_and_tells_the_driver`; `test_no_slot_there_keeps_the_original_and_opens_a_case`; `test_partner_destination_sends_a_request_and_never_says_booked`; `test_other_facility_warehouse_gets_404`; `test_page_lists_pending_requests_and_counts_them`; `test_rejection_tells_the_driver_and_keeps_the_booking`.
- [ ] **Step 3:** Implement routes and follow-up. If approval is followed by `NO_SLOT`, set the destination back to the original in the same operation and record the request as REJECTED with a reason, so the load is never left pointing at a site with no booking.
- [ ] **Step 4:** `pytest -q`. Expected: PASS.
- [ ] **Step 5:** Commit `feat: warehouse approves facility changes`.

### Task 7: Documents and verification

**Files:** Modify `README.md`, `docs/running-and-demonstrating.md`, `docs/demonstration-guide.md`, `docs/scheduling-algorithm.md`; Create `docs/verification/auto-hold-facility-run.md`.

- [ ] **Step 1:** Update the walkthroughs (no more numbered menu; the change-facility flow).
- [ ] **Step 2:** Run `ruff check . && mypy src && pytest -q`. Expected: all PASS.
- [ ] **Step 3:** Smoke in the demo with the stub model: delay, No, Yes, change request, approve, confirm; record commands and results, including failures.
- [ ] **Step 4:** Commit `docs: auto-hold and facility change`.
