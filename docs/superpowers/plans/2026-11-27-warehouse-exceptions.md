# Warehouse Exceptions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Warehouse staff can block a dock, free a booked slot from the dock board, and delete (and restore) a driver.

**Architecture:** One shared "free a booking" module is used by all three features. Dock blocks are `dock_status_events` rows plus a companion table that capacity reads to end a block early. Each feature is a service module with thin routes and server-rendered screen parts.

**Tech Stack:** Python 3.12, FastAPI, SQLite, server-rendered HTML, plain JavaScript, pytest.

**Spec:** `docs/superpowers/specs/2026-11-27-warehouse-exceptions-design.md` (read it with this plan).

## Global Constraints

- Branch: `feat/shipment-assignment`. No new dependencies.
- Writes happen inside `setuhaul.db.immediate`; `queue_driver_notice` commits, so tell drivers only **after** the transaction.
- Admins act at any facility, `WAREHOUSE_AUTHORITY` only at their own, `SECURITY` never. Out of scope answers like "not found" (404). Partner facilities (`is_partner`) are excluded.
- Every new route is added to `ROLE_GUARDED` in `tests/test_auth_role_matrix.py`.
- `appointment_events`, `audit_log` and `escalation_events` are append-only. Nothing deletes a driver, shipment, appointment or event row.
- Never free `IN_PROGRESS` appointments or shipments whose truck is inside the gate.
- Form times without an offset are India time.
- Page text goes through `esc()`; scripts write with `textContent` only (CSP: no inline script or style).
- Driver messages exist in `en`, `hi`, `hinglish` in `language.T` (`tests/test_language.py` enforces it).
- New code carries comments in the style of the surrounding files.
- Test command: `.venv\Scripts\python.exe -m pytest <files> -q`. Full check at the end: `py -3.12 scripts/ci.py`. GitHub CI stays off.

## Review Focus

1. A stale page or double click frees an already freed booking: `409 NOT_BOOKED`, no second message or event (Task 1).
2. Two blocks overlap and one is reopened: the other stays in force (Task 2).
3. Two affected shipments and one free slot: one moves, the other is "none fits" (Task 3).
4. Telegram not configured or driver not linked: the action succeeds and the screen names who was not reached (Tasks 3, 4).
5. A shipment is added between the delete warning and the confirmation: `409 PLAN_CHANGED`, nothing changes (Task 6).

## Files

| File | Responsibility |
|---|---|
| `src/setuhaul/release.py` (new) | Find, check, free a booking; tell the driver |
| `src/setuhaul/dock_blocks.py` (new) | Block, plan moves, reopen, list |
| `src/setuhaul/auth/driver_removal.py` (new) | Preview, delete, restore |
| `src/setuhaul/auth/exception_routes.py` (new) | Routes for all three |
| `src/setuhaul/auth/exception_pages.py` (new) | Block form and list, deleted drivers list |
| `src/setuhaul/auth/assets/exceptions.js` (new) | Warning-then-confirm for block and delete |
| `migrations/018_dock_blocks.sql`, `migrations/019_driver_deletions.sql` (new) | Companion tables |
| `capacity.py`, `board.py`, `auth/pages.py`, `auth/shipment_pages.py`, `language.py` (modify) | Read block ends; show reasons and cross; hide blocked slots; wording |

## Shared test fixture

Create `tests/exception_helpers.py` in Task 1 and reuse it.

```python
from setuhaul.clock import FakeClock
from setuhaul.db import parse_operational_timestamp as ts
from tests.auth_helpers import build_world
from tests.shipment_helpers import NOW, create, open_av, query, slot_at

D1, D2 = "DOCK-JAI-D1", "DOCK-JAI-D2"

def booked_world(tmp_path, to_time="13:00"):
    """Two standard docks open on DAY and one shipment booked on D1 at 09:00."""
    world = build_world(tmp_path, FakeClock(ts(NOW)))
    open_av(world, dock_ids=[D1, D2], to_time=to_time)
    shipment_id = create(world, slot_at(world, 9, D1))
    appointment_id = query(world, "select appointment_id from appointments where shipment_id=? and is_current=1", shipment_id)[0][0]
    return world, shipment_id, appointment_id
```

---

### Task 1: Shared "free a booking" step

**Files:** Create `src/setuhaul/release.py`, `tests/exception_helpers.py`, `tests/test_release.py`. Modify `src/setuhaul/language.py`.

**Interfaces (produces):**

```python
Cause = Literal["SLOT_FREED", "DOCK_BLOCKED", "DRIVER_DELETED"]   # stored as cancellation_reason

@dataclass(frozen=True)
class Booking:
    appointment_id: str; shipment_id: str; driver_id: str; driver_name: str; order_reference: str
    facility_id: str; dock_id: str; dock_code: str; slot_id: str; start: datetime; end: datetime; status: str

def booking(conn, appointment_id: str) -> Booking | None
def truck_inside(conn, shipment_id: str) -> bool
def not_freeable(conn, found: Booking) -> str | None          # "NOT_BOOKED" | "UNLOADING" | "TRUCK_ON_SITE"
def free_in_txn(conn, found: Booking, cause: Cause, actor: User, clock: Clock, *, announce: bool = True) -> None
def free_slot(conn, *, actor: User, appointment_id: str, clock: Clock) -> Booking     # opens its own transaction
def tell_driver(conn, clock, telegram_on: bool, found: Booking, key: str, **fields: str) -> bool   # True if it can reach the phone
```

Rules: only `CONFIRMED` and `PENDING_CONFIRMATION` are freeable. `truck_inside` is a truck visit in `CHECKED_IN` or `OPERATIONS_COMPLETE`, or shipment status `AT_GATE`, `WAITING` or `IN_DOCK`. `free_in_txn` releases the shipment's holds (`booking.release_hold_in_txn`), sets the appointment `CANCELLED`, not current, appends `APPOINTMENT_CANCELLED` with actor `WAREHOUSE`, calls `enqueue_downstream` when `announce`, and writes audit action `BOOKING_FREED`. It does not touch the gate pass (`gate._assess` already refuses with `NO_APPOINTMENT`). `free_slot` also refuses an out-of-scope or partner booking (`APPOINTMENT_NOT_FOUND`) and a started slot (`SLOT_STARTED`). `tell_driver` appends to the open chat thread and queues the notice, as `facility_change._tell` does; it returns `telegram_on and passes.bound_chat(...) is not None`.

Wording key `slot_freed`, fields `order, dock, start, date`; English: "For order {order}: the warehouse has freed your booking at dock {dock}, {start} on {date}. You have no slot now. Reply here to get a new one."

- [ ] **Step 1: Write the failing tests** (`tests/test_release.py`)

```python
def test_free_slot_cancels_the_booking_and_keeps_the_shipment(tmp_path):
    world, shipment_id, appt = booked_world(tmp_path)
    found = run(world, release.free_slot, actor=world.users["wh_a"], appointment_id=appt)
    row = query(world, "select appointment_status, is_current, cancellation_reason from appointments where appointment_id=?", appt)[0]
    assert tuple(row) == ("CANCELLED", 0, "SLOT_FREED") and found.shipment_id == shipment_id
    assert query(world, "select current_status from shipments where shipment_id=?", shipment_id)[0][0] == "ASSIGNED"
    assert count(world, "appointment_events", f"appointment_id='{appt}' and event_type='APPOINTMENT_CANCELLED'") == 1
    assert count(world, "audit_log", "action='BOOKING_FREED'") == 1

def test_freeing_twice_is_refused_and_changes_nothing(tmp_path):      # second call: AuthError code "NOT_BOOKED"; event count stays 1
def test_another_warehouse_cannot_see_the_booking(tmp_path):          # actor wh_b: "APPOINTMENT_NOT_FOUND"
def test_a_started_slot_is_refused(tmp_path):                         # clock advanced to 2026-11-28T09:05+05:30: "SLOT_STARTED"
def test_unloading_and_on_site_are_refused(tmp_path):                 # IN_PROGRESS -> "UNLOADING"; shipment AT_GATE -> "TRUCK_ON_SITE"
def test_tell_driver_queues_a_notice_and_reports_reachability(tmp_path):
    # telegram_on=False returns False; one notification_outbox row for recipient_type 'DRIVER' is still queued
```

`run(world, fn, **kw)` is a small helper in the test file: open a connection to `world.path`, call `fn(conn, clock=world.clock, **kw)`, close.

- [ ] **Step 2:** Run `... -m pytest tests/test_release.py -q`. Expected: FAIL, `No module named 'setuhaul.release'`.
- [ ] **Step 3:** Implement `release.py` and the `slot_freed` wording.
- [ ] **Step 4:** Run `... tests/test_release.py tests/test_language.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit with the spec and this plan: `feat: add the shared free-a-booking step`.

---

### Task 2: Store dock blocks and reopen them

**Files:** Create `migrations/018_dock_blocks.sql`, `src/setuhaul/dock_blocks.py`, `tests/test_dock_blocks.py`. Modify `src/setuhaul/capacity.py:213-223`.

**Interfaces:**
- Table `dock_blocks(dock_event_id TEXT PRIMARY KEY REFERENCES dock_status_events, created_by TEXT REFERENCES users, created_at TEXT, ended_by TEXT REFERENCES users, ended_at TEXT)`. A row for a sample event is created only when it is reopened (`created_by` empty).
- Produces:

```python
class BlockRequest(BaseModel):          # extra="forbid"
    facility_id: str; dock_id: str
    event_type: Literal["MANUAL_BLOCK", "MAINTENANCE", "BREAKDOWN"]
    starts: datetime; ends: datetime | None = None
    reason: str = Field(min_length=1, max_length=200)

def insert_block_in_txn(conn, actor: User, data: BlockRequest, clock: Clock) -> str   # new dock_event_id "DEVT-<hex>"
def reopen(conn, *, actor: User, dock_event_id: str, clock: Clock) -> None
def current(conn, facility_ids: Sequence[str], now: datetime) -> list[dict]           # not ended, not past; sample events included
```

Rules: refusals `FACILITY_NOT_FOUND`, `PARTNER_FACILITY`, `DOCK_NOT_FOUND`, `INVALID_TIMES` (`ends` not after both `starts` and now), `REASON_REQUIRED` (blank), `BLOCK_NOT_FOUND` (unknown, out of scope, already over or reopened). `build_facility_state` left-joins `dock_blocks`: the effective end is the earlier of `event_end_ts` and `ended_at`; an event whose effective end is not after its start is left out. `reopen` never edits the event row and never writes a `REOPENED` event. It sets stored `BLOCKED` slots (clearing `block_reason`) back to `OPEN` when they are on that dock, overlap the event, end after now and no other block in force overlaps them. Audit actions `DOCK_BLOCKED`, `DOCK_REOPENED`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_block_makes_the_slot_blocked(tmp_path):        # insert D2 10:00-12:00; board cells 10,11 are BLOCKED, 12 AVAILABLE
def test_reopening_one_of_two_blocks_keeps_the_other(tmp_path):
    # blocks 10:00-12:00 and 11:00-13:00 on D2; reopen the first: 10 AVAILABLE, 11 and 12 BLOCKED; no 'REOPENED' row exists
def test_reopening_a_future_open_ended_block_removes_it(tmp_path):
def test_form_times_without_offset_are_india_time(tmp_path):   # stored event_start_ts ends with "+05:30" and keeps the hour
def test_end_must_be_after_start_and_now(tmp_path):            # "INVALID_TIMES"
def test_other_warehouse_cannot_block_or_reopen(tmp_path):
def test_reopening_the_sample_breakdown_opens_its_slots(tmp_path):
    # clock 2026-08-04T09:35: reopen DEVT001 -> SLOT-JAI-031..033 slot_status 'OPEN', block_reason NULL; DEVT001 row unchanged
```

- [ ] **Step 2:** Run `... tests/test_dock_blocks.py -q`. Expected: FAIL on import.
- [ ] **Step 3:** Add the migration, the capacity join and the three functions.
- [ ] **Step 4:** Run `... tests/test_dock_blocks.py tests/test_feasibility.py tests/test_board.py tests/test_audit.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat: store dock blocks and reopen them`.

---

### Task 3: Warn, move and free

**Files:** Modify `src/setuhaul/dock_blocks.py`, `src/setuhaul/language.py`. Test `tests/test_dock_blocks.py`.

**Interfaces:**
- Consumes: Task 1 `release.*`, Task 2 `insert_block_in_txn`.
- Produces:

```python
class PlanItem(BaseModel):              # frozen
    appointment_id: str; shipment_id: str; order_reference: str; driver_name: str
    dock_code: str; start: str
    action: Literal["MOVE", "FREE", "SKIP"]
    new_slot_id: str | None = None; new_dock_code: str | None = None; new_start: str | None = None
    why: str | None = None              # for SKIP: "UNLOADING" | "TRUCK_ON_SITE"

class Expected(BaseModel):
    appointment_id: str; new_slot_id: str | None = None

class PlanChanged(AuthError):           # code "PLAN_CHANGED", .items: list[PlanItem]
class BlockResult(BaseModel):
    dock_event_id: str; items: list[PlanItem]; unreachable: list[str]   # order references

def preview(conn, *, actor: User, data: BlockRequest, clock: Clock) -> list[PlanItem]
def block(conn, *, actor: User, data: BlockRequest, expected: list[Expected], clock: Clock, telegram_on: bool) -> BlockResult
```

Planning rules:
- Affected: `CONFIRMED`/`PENDING_CONFIRMATION`/`IN_PROGRESS` appointments on the dock whose slot overlaps the period and ends after now.
- Order: priority `CRITICAL, HIGH, NORMAL, LOW`, then original start.
- Candidates: other docks of the same facility, `start > now`, `evaluate_slot` status not `INFEASIBLE`/`INCOMPLETE_FEASIBILITY_DATA` (the rule in `create_shipment`). ETA is `max(latest_eta_ts or original_eta_ts or booked start, now)`, built as in `create_shipment`.
- The proposed block is appended to `state.events` before judging. After each choice, update `state.occupancy` in memory (drop the old slot, add the new one) so no slot is proposed twice.
- Pick the smallest `(abs(start - original), start, dock_code, slot_id)`.

Applying: one transaction. Recompute the plan; if the MOVE/FREE pairs differ from `expected`, raise `PlanChanged` and save nothing. Insert the block; release holds on blocked slots; for each item `free_in_txn(..., "DOCK_BLOCKED", announce=action=="FREE")`; for MOVE insert a `CONFIRMED`, current, `booking_source='WAREHOUSE'` appointment with `replaced_appointment_id`, append `RESCHEDULE_CONFIRMED` with actor `WAREHOUSE`, `enqueue_downstream`, `passes.sync_window`. Tell drivers after commit with `dock_moved` (order, dock, start, date, old_dock), `slot_freed`, or `hold_blocked` (order, dock).

English: `dock_moved` = "For order {order}: dock {old_dock} is out of use, so the warehouse has moved you to dock {dock} at {start} on {date}. Reply here if you cannot make it."; `hold_blocked` = "For order {order}: dock {dock} is out of use, so the slot held for you is released."

- [ ] **Step 1: Write the failing tests**

```python
def test_preview_proposes_the_closest_slot_on_another_dock(tmp_path):
    # block D1 09:00-10:00: one MOVE item, new_dock_code "D2", new_start 09:00 on DAY; nothing written
def test_block_moves_the_booking(tmp_path):
    # old row CANCELLED 'DOCK_BLOCKED'; new row CONFIRMED, current, replaced_appointment_id=old; one DRIVER notice naming D2
def test_two_shipments_never_get_the_same_slot(tmp_path):
    # open only 09:00-10:00; two shipments, on D1 and ... second blocked-dock booking: one MOVE, one FREE (use two D1 slots with to_time "11:00" and D2 10:00 taken)
def test_nothing_fits_frees_and_keeps_the_shipment(tmp_path):
def test_a_changed_plan_saves_nothing(tmp_path):          # expected=[] -> PlanChanged; no dock_blocks row, booking unchanged
def test_a_truck_on_site_is_listed_and_left(tmp_path):    # action "SKIP", why "TRUCK_ON_SITE"
def test_a_failure_part_way_saves_nothing(tmp_path, monkeypatch):  # make passes.sync_window raise; assert no change
def test_unreachable_drivers_are_named(tmp_path):         # telegram_on=False -> unreachable == ["ORD-261127-001"]
```

For `test_two_shipments_never_get_the_same_slot`: `booked_world(to_time="11:00")`, create a second shipment (`order_number="ORD-261127-002"`) on D1 10:00 and a third (`"ORD-261127-003"`) on D2 10:00, then block D1 09:00-11:00. Only D2 09:00 is free, so the actions sorted are `["FREE", "MOVE"]`.

- [ ] **Step 2:** Run them. Expected: FAIL, `preview` undefined.
- [ ] **Step 3:** Implement `preview`, `block`, wording in three languages.
- [ ] **Step 4:** Run `... tests/test_dock_blocks.py tests/test_language.py tests/test_pass_changes.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat: move or free bookings when a dock is blocked`.

---

### Task 4: Block routes and screen

**Files:** Create `auth/exception_routes.py`, `auth/exception_pages.py`, `auth/assets/exceptions.js`, `tests/test_exception_routes.py`. Modify `api.py` (register beside `register_shipment_routes`), `auth/pages.py` (`ASSET_TYPES`, `_board_section`, script list), `board.py:34-47`, `auth/shipment_pages.py:50-63`, `tests/test_auth_role_matrix.py`.

**Interfaces (produces):**
- `register_exception_routes(app, deps)`; same error shape as `shipment_routes` (`{"status": "REJECTED", "reason_code", "message"}`; `*_NOT_FOUND` 404; `PLAN_CHANGED`, `NOT_BOOKED`, `SLOT_STARTED`, `UNLOADING`, `TRUCK_ON_SITE` 409 with `items` for `PLAN_CHANGED`; else 422). Every code has a sentence.
- `POST /v1/warehouse/dock-blocks/preview` body `BlockRequest` -> `{items}`
- `POST /v1/warehouse/dock-blocks` body `BlockRequest` + `expected` -> 201 `{dock_event_id, items, unreachable}`
- `POST /v1/warehouse/dock-blocks/{dock_event_id}/reopen`
- `exception_pages.blocks_section(conn, user, facility: str, now) -> str`, rendered inside the Dock board section: form `id="block-form"` and the current blocks with **Reopen** (`_button(..., confirm=...)`).
- `exceptions.js`: submit calls preview, writes the affected list with `textContent` into the form, shows **Block and move** (or **Block** when nothing is affected), posts with `expected`; on `PLAN_CHANGED` redraws the list; after success shows "Could not reach: ..." for `unreachable`, kept across reload with `sessionStorage` as `warehouse.js` does for availability. Editing any field discards the preview.

Other rules: `build_board` gives an unoccupied blocked cell the overlapping event's reason as `label` when the slot has no `block_reason`. The new-shipment slot list drops slots overlapped by `state.effective_events`.

- [ ] **Step 1: Write the failing tests**

```python
def test_preview_then_block_over_http(tmp_path):          # 200 with one item; then 201; board API shows label == reason
def test_stale_expected_answers_409_with_the_new_list(tmp_path):
def test_other_warehouse_gets_404(tmp_path):
def test_the_page_has_the_block_form_and_reopen_button(tmp_path):  # GET /warehouse?facility=FAC-JAI-01 contains 'id="block-form"'
def test_blocked_slots_are_not_offered_for_new_shipments(tmp_path):  # the slot id is absent from the shipments form
```

Add the three routes to `ROLE_GUARDED` with `BOTH`.

- [ ] **Step 2:** Run `... tests/test_exception_routes.py tests/test_auth_role_matrix.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run those plus `tests/test_board.py tests/test_warehouse_screen.py tests/test_auth_pages.py tests/test_shipment_routes.py`. Expected: PASS.
- [ ] **Step 5:** In the browser (`scripts/run_demo.py --auto-login`, then `/warehouse`): block a dock, see the warning, confirm, reopen. No console errors.
- [ ] **Step 6:** Commit: `feat: block docks from the warehouse screen`.

---

### Task 5: Cross on the dock board

**Files:** Modify `board.py`, `auth/pages.py:497-518`, `auth/exception_routes.py`, `auth/assets/app.css`, `tests/test_auth_role_matrix.py`. Test `tests/test_exception_routes.py`.

**Interfaces:**
- Consumes: `release.free_slot`, `release.not_freeable`, `release.tell_driver`.
- Board cell gains `appointment_id: str | None` and `freeable: bool` (booked, `start > now`, `not_freeable` is `None`; false for partner facilities).
- `POST /v1/warehouse/appointments/{appointment_id}/free` -> `{appointment_id, reached: bool}`; tells the driver `slot_freed`.
- A freeable cell holds `<button type="button" class="free-slot" data-api=... data-confirm="Free {dock} {HH:MM} for {order} ({name})? The shipment stays and will need a new slot." aria-label="Free dock {dock} {HH:MM}, order {order}">×</button>`. The click listener in `common.js` is on the document, so it survives the live refresh. Not-reached is reported by the same `sessionStorage` line as Task 4.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_booked_future_cell_has_a_cross_with_a_label(tmp_path):
def test_no_cross_on_held_started_on_site_or_unloading_cells(tmp_path):
def test_free_route_frees_and_then_answers_409(tmp_path):   # 200, then 409 NOT_BOOKED, still one DRIVER notice
```

- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement; add the route to `ROLE_GUARDED`.
- [ ] **Step 4:** Run `... tests/test_exception_routes.py tests/test_board.py tests/test_warehouse_screen.py tests/test_auth_role_matrix.py tests/test_demo_ui.py -q`. Expected: PASS.
- [ ] **Step 5:** In the browser: free a slot, wait for a live refresh, free another.
- [ ] **Step 6:** Commit: `feat: free a booked slot from the dock board`.

---

### Task 6: Delete and restore a driver (service)

**Files:** Create `migrations/019_driver_deletions.sql`, `auth/driver_removal.py`, `tests/test_driver_removal.py`. Modify `auth/warehouse_routes.py` (`drivers_in_scope`), `auth/onboarding.py` (`add_driver`, `_decide`, `pending`, `decided`), `auth/telegram.py` (`may_manage_binding`).

**Interfaces:**
- Table `driver_deletions(deletion_id INTEGER PRIMARY KEY AUTOINCREMENT, driver_id TEXT NOT NULL REFERENCES drivers, deleted_by TEXT NOT NULL REFERENCES users, deleted_at TEXT NOT NULL, previous_status TEXT NOT NULL, restored_by TEXT REFERENCES users, restored_at TEXT)`, unique on `driver_id` where `restored_at is null`.
- Produces:

```python
class Doomed(BaseModel):   shipment_id: str; order_reference: str; dock_code: str | None; start: str | None; blocked_by: str | None
class Preview(BaseModel):  driver_id: str; driver_name: str; shipments: list[Doomed]
def is_deleted(conn, driver_id: str) -> bool
def preview(conn, *, actor: User, driver_id: str) -> Preview
def delete(conn, *, actor: User, driver_id: str, confirm_name: str, expected: list[str], clock: Clock) -> Preview
def restore(conn, *, actor: User, driver_id: str, clock: Clock) -> None
def deleted(conn) -> list[sqlite3.Row]
```

Rules:
- Who: `telegram.may_manage_binding` before filtering deleted ones; otherwise `DRIVER_NOT_FOUND`. A warehouse authority with an unfinished shipment of this driver outside their facilities: `OTHER_FACILITY_SHIPMENTS`.
- Unfinished: `current_status not in ('COMPLETED','CANCELLED')`.
- `blocked_by`: `UNLOADING` or `TRUCK_ON_SITE` (`release.truck_inside`). Any blocked shipment refuses the whole delete with `DRIVER_ON_SITE`.
- `confirm_name` must equal the name (stripped, case-insensitive): `NAME_MISMATCH`. `sorted(expected)` must equal the unfinished shipment ids: `PLAN_CHANGED`.
- One transaction: for each shipment free its booking (`release.free_in_txn(..., "DRIVER_DELETED")`) and any holds, `passes.cancel`, set `CANCELLED`, set open `partner_requests` and `facility_change_requests` to `EXPIRED`; set `driver_status='INACTIVE'`; revoke the Telegram row; set `released_at` on project allocations; insert the deletion row; audit `DRIVER_DELETED`. No driver message.
- Restore: admin only (`FORBIDDEN`), `NOT_DELETED` otherwise; restore `previous_status`; audit `DRIVER_RESTORED`.
- Elsewhere: deleted drivers are left out of `drivers_in_scope`, `pending`, `decided`; `_decide` raises `DRIVER_DELETED`; `may_manage_binding` is false for them; `add_driver` matching a deleted driver's phone or licence raises `DRIVER_DELETED_RESTORE`.

- [ ] **Step 1: Write the failing tests**

```python
def test_delete_cancels_shipments_frees_slots_and_hides_the_driver(tmp_path):
    # shipment CANCELLED, appointment CANCELLED 'DRIVER_DELETED', driver INACTIVE, absent from drivers_in_scope,
    # driver_eligible False, resolve_eligible_driver(phone) raises AuthError, driver row still exists
def test_a_new_shipment_after_the_warning_changes_nothing(tmp_path):   # "PLAN_CHANGED"
def test_wrong_name_and_on_site_are_refused(tmp_path):
def test_warehouse_scope_rules(tmp_path):      # not their driver -> DRIVER_NOT_FOUND; shipment elsewhere -> OTHER_FACILITY_SHIPMENTS
def test_a_deleted_driver_cannot_be_approved_or_added_again(tmp_path):
def test_restore_brings_the_driver_back_but_not_the_shipments(tmp_path):   # eligible again; shipment stays CANCELLED
```

- [ ] **Step 2:** Run `... tests/test_driver_removal.py -q`. Expected: FAIL on import.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run it with `tests/test_auth_onboarding.py tests/test_telegram_binding.py tests/test_shipment_create.py tests/test_warehouse_drivers_api.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat: delete and restore drivers`.

---

### Task 7: Delete and restore screens, and docs

**Files:** Modify `auth/exception_routes.py`, `auth/exception_pages.py`, `auth/assets/exceptions.js`, `auth/pages.py` (`_drivers_section`, admin tabs and body), `tests/test_auth_role_matrix.py`, `docs/demonstration-guide.md`, `docs/running-and-demonstrating.md`. Test `tests/test_exception_routes.py`.

**Interfaces:**
- `GET /v1/warehouse/drivers/{driver_id}/deletion` -> `Preview`
- `POST /v1/warehouse/drivers/{driver_id}/delete` body `{confirm_name, expected: [shipment_id]}`
- `POST /v1/drivers/{driver_id}/restore` (`ADMIN`)
- `PLAN_CHANGED`, `DRIVER_ON_SITE`, `NOT_DELETED` are 409.
- Drivers table: a **Delete** button (only where allowed) opens a panel under the table listing each shipment with dock and time or "no slot booked", a name field, and **Delete driver**. The live refresh does not replace this section.
- `exception_pages.deleted_section(conn) -> str`: admin tab `("deleted", "Deleted drivers")` with **Restore** buttons. `admin.js` is not changed; the admin page already loads `common.js`.

- [ ] **Step 1: Write the failing tests**

```python
def test_delete_over_http_after_reading_the_warning(tmp_path):
def test_restore_is_admin_only(tmp_path):                     # wh_a 403, admin 200
def test_delete_button_only_where_allowed(tmp_path):
def test_admin_page_lists_deleted_drivers(tmp_path):
```

Add the three routes to `ROLE_GUARDED` (`BOTH`, `BOTH`, `ADMIN`).

- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement; add a short "Warehouse exceptions" section to both docs covering block, reopen, cross, delete, restore, and correct the demo guide's "The board has no release button" (it stays true for the demo console only).
- [ ] **Step 4:** Run `py -3.12 scripts/ci.py`. Expected: ruff, mypy and all tests pass.
- [ ] **Step 5:** In the browser: delete a test driver, then restore them from `/admin`.
- [ ] **Step 6:** Commit: `feat: delete drivers from the warehouse screen`.
