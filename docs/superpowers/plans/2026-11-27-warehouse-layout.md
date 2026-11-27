# Warehouse Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The warehouse Dock board tab shows a compact board with an Operations panel beside it, like the demo console.

**Architecture:** Server-rendered HTML as today. `_board_section` draws the compact board and wraps the operations panel;
`warehouse.js` handles the detail box and panel views and keeps them across live refreshes.

**Tech Stack:** Python 3.12, server-rendered HTML, plain JavaScript, CSS, pytest.

**Spec:** `docs/superpowers/specs/2026-11-27-warehouse-layout-design.md`

## Global Constraints

- New branch `feat/warehouse-layout` from `feat/wiki`. Do not push. Commit only this plan's files.
- No inline script or style; text through `esc()`; scripts write with `textContent`.
- `demo/*` and routes are unchanged. Roles and facility scope are unchanged.
- Colours come from `tokens.css`.
- Keep the element ids and classes the live refresh uses unless a task says otherwise: `.wh .attention`, `#board .asof`,
  `#board .bays`, `#board .scroll`, `#board .blocks-list`, `#messages`.
- Update existing tests only where they assert the old layout; say which in the commit message.
- Finish with `py -3.12 scripts/ci.py`.

## Review Focus

1. A live refresh while a case note is half typed, or the detail box is open: nothing typed is lost and the box does not act
   on a slot that has since changed (Task 2 browser step).
2. A cell with two slots in one hour (partner bookings start mid-hour): both are reachable (Task 1 test).
3. A blocked cell that also has a booking: the detail box shows both the reason and the booking (Task 1 test).
4. Keyboard only: open a cell, free it, return to the board (Task 1 browser step).
5. An old `#messages` bookmark opens the board with the panel visible (Task 2 test and browser step).

---

### Task 1: Compact board, detail box, facility buttons

**Files:** Modify `src/setuhaul/auth/pages.py` (`_board_section`, `_cross`), `auth/assets/app.css`,
`auth/assets/warehouse.js`; tests `tests/test_warehouse_screen.py`, `tests/test_exception_routes.py`.

**Interfaces (produces):**
- Each slot is `<button type="button" class="slot" data-key="slot:{slot_id}" data-detail="{text}" title="{text}"
  aria-label="{text}">` holding the driver's initials (`BOOKED`/`HELD`), or nothing. `{text}` is
  `"{dock} {start}-{end}: {state}"`, plus `", {driver}, {order}"` when occupied and `", {reason}"` when blocked.
- A freeable slot also has `data-free="/v1/warehouse/appointments/{id}/free"` and `data-confirm="..."` (the wording used now).
- One `<dialog id="slot-detail">`, outside every live-refreshed element, with the text, a **Free this slot** button (hidden
  when not freeable; it gets `data-api` and `data-confirm` so `common.js` handles it) and **Close**.
- Facility choice: links `?facility={id}` with `aria-current` for up to five facilities, otherwise the present form. The live
  refresh reads the shown facility from `#board [data-facility]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_cells_show_initials_and_carry_the_detail():       # "Manoj Sharma · ORD-1" booked: button text "MS", aria-label has name and order
def test_two_slots_in_one_hour_are_both_buttons():
def test_a_blocked_cell_with_a_booking_names_both():       # reason and driver in data-detail
def test_free_cells_have_no_visible_word_but_a_name():     # empty button, aria-label ends ": Free"
def test_freeable_slot_carries_the_free_address_and_question():
def test_facilities_are_buttons_and_six_fall_back_to_a_list():
```

Replace the assertions in `test_exception_routes.py` that look for the old × button with `data-free`.

- [ ] **Step 2:** Run `.venv\Scripts\python.exe -m pytest tests/test_warehouse_screen.py tests/test_exception_routes.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement. Cells are at least 24 by 32 px; the table uses `table-layout: fixed` with a 5.5rem dock column.
- [ ] **Step 4:** Same command plus `tests/test_board.py tests/test_screen_states.py tests/test_shared_look.py`. Expected: PASS.
- [ ] **Step 5:** Browser (demo, `.env` off) at 1366 and 1920 px: no sideways scroll; open a cell by keyboard, free it, focus
  returns to the board.
- [ ] **Step 6:** Commit: `feat: compact warehouse dock board with a slot detail box`.

---

### Task 2: Operations panel

**Files:** Modify `auth/pages.py` (`_messages_section`, `_board_section`, `render_warehouse`, `_attention`, `_admin_attention`
links), `auth/assets/app.css`, `warehouse.js`, `tabs.js`; tests `tests/test_warehouse_screen.py`, `tests/test_auth_pages.py`,
`tests/test_waiting_work.py`, `tests/test_warehouse_cases.py`.

**Interfaces:**
- Consumes: Task 1 layout.
- `<aside id="messages" class="ops">` inside the board section, beside the board, holding buttons
  `data-view="cases|chats|requests"` (each with a count) and matching `<div data-pane="...">`.
- Cases: open cases and recently resolved. Chats: the chat browser, list above thread. Requests: facility change requests,
  partner slot requests, waiting for partner.
- Below the board: `<details data-key="bays">` and `<details data-key="blocking">`.
- Tabs are `board, projects, shipments, drivers`; the badge for waiting work moves to the Dock board tab and the Cases view.
- `tabs.js`: `#messages` shows the `board` panel.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_board_tab_holds_the_operations_panel(world):   # <aside id="messages" inside data-panel="board"
def test_tabs_are_four_and_there_is_no_messages_tab(world):
def test_each_view_has_its_content(world):                  # a case card in cases, partner request in requests
def test_attention_cards_still_point_at_messages(world):
```

- [ ] **Step 2:** Run those test files. Expected: FAIL.
- [ ] **Step 3:** Implement. Columns: `minmax(0, 1fr) 22rem` above 75rem, one column below. The chosen view and folded sections
  survive a live refresh (the existing `remember` function).
- [ ] **Step 4:** Run them plus `tests/test_screen_states.py tests/test_screen_messages.py tests/test_driver_chat_window.py`. Expected: PASS.
- [ ] **Step 5:** Browser at 375, 1366 and 1920 px: claim and resolve a case from the panel; type a note and wait for two
  refreshes; open `/warehouse#messages`.
- [ ] **Step 6:** Commit: `feat: operations panel beside the warehouse board`.

---

### Task 3: Wiki and documents

**Files:** Modify `auth/wiki_pages.py`, `docs/demonstration-guide.md`, `docs/running-and-demonstrating.md`; test `tests/test_wiki.py`.

- [ ] **Step 1:** Add a test that no wiki step mentions "Messages tab" or "small ×", and that "Free this slot" and "Operations"
  appear. Run: FAIL.
- [ ] **Step 2:** Update the wording and both documents. Run `tests/test_wiki.py`: PASS.
- [ ] **Step 3:** `py -3.12 scripts/ci.py` passes. Commit: `docs: describe the new warehouse layout`.
