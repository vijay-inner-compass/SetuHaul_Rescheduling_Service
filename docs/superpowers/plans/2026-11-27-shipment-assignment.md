# Shipment Assignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Warehouse users and admins create and assign shipments on screen; a driver sends the order number once on Telegram before the bot can show or book that shipment.

**Architecture:** A `shipment_assignments` table marks app-created shipments, and the view `v_driver_shipments` hides unconfirmed ones from every driver-facing read. The app matches order numbers in the shared message pipeline, before the model. A new Warehouse **Shipments** tab opens availability and creates shipments.

**Tech Stack:** Python 3.12, FastAPI, SQLite migrations, server-rendered pages with the existing `data-api` forms, pytest.

**Spec:** `docs/superpowers/specs/2026-11-27-shipment-assignment-design.md`

## Global Constraints

- Work on branch `feat/shipment-assignment`, created from `main`. Commit the spec and this plan first.
- Order number: `ORD` followed by at least six digits, hyphens or spaces allowed. Case, spaces and hyphens ignored when matching; unique on that basis.
- Five wrong numbers lock confirmation and open a case.
- Availability: at most 14 days at a time; slot length default 60 minutes.
- Rows written here carry `source='WAREHOUSE_UI'` (labour, consignee) or `booking_source='WAREHOUSE'` (appointment).
- A shipment with no assignment row is treated as confirmed.
- No order number, id, destination or time of an unconfirmed shipment reaches the model or the driver.
- Writes: `require_role("WAREHOUSE_AUTHORITY", "ADMIN")`, facility scope, `immediate()` transaction, `audit.record`. Out of scope answers like not found.
- Fixed driver replies exist in `en`, `hi`, `hinglish` (`language.T`).
- Times are IST. A naive form value is read as IST.
- Follow the codebase's comment style. Tests: `.venv\Scripts\python.exe -m pytest`.

## Review Focus

1. `ORD-261127-001 5 pm` confirms; the trailing 5 does not spoil the match (Task 2).
2. Digits without `ORD` (`261127001`, a phone number, `1730`) are not attempts; the waiting prompt still appears (Tasks 2, 3).
3. After reassignment or cancellation, the old driver's correct number does not confirm anything (Task 5).
4. Two creations choosing the same slot: the second gets `SLOT_NOT_AVAILABLE` and leaves no rows (Task 4).
5. Submitting the same availability twice creates no duplicate slot or labour row (Task 4).

## File Structure

| File | Responsibility |
|---|---|
| `migrations/016_shipment_assignments.sql` | table and view |
| `src/setuhaul/order_numbers.py` | parsing and normalising (pure) |
| `src/setuhaul/order_check.py` | confirm, count, lock |
| `src/setuhaul/auth/shipments.py` | warehouse operations |
| `src/setuhaul/auth/shipment_routes.py` | HTTP |
| `src/setuhaul/auth/pages.py` | Shipments tab |
| `tests/shipment_helpers.py` | shared fixtures |

---

### Task 1: Assignment table and driver visibility

**Files:**
- Create: `migrations/016_shipment_assignments.sql`, `tests/shipment_helpers.py`, `tests/test_shipment_visibility.py`
- Modify: `identity.py` (`authorize_shipment`), `memory.py` (`_owns_shipment`, `_active_shipments`), `tools.py` (`_list_my_shipments`, `_has_partner_shipment`), `cancellation.py` (`_require_shipment` and other shipment reads), `gps.py` (`open_shipments_for_driver`), `position.py` (`handle_new_position`), `facility_change.py` (`request_change`), `partner_booking.py` (request creation), `option_history.py` (owner check)

**Interfaces:**
- Produces: table `shipment_assignments(shipment_id text primary key references shipments, created_by text not null references users(user_id), created_at text not null, confirmed_at text, failed_attempts integer not null default 0 check (failed_attempts >= 0), locked_at text)`
- Produces: view `v_driver_shipments` = every `shipments` column, excluding rows whose assignment has `confirmed_at is null`
- Produces: `tests.shipment_helpers.mark_unconfirmed(conn, shipment_id: str, created_by: str) -> None` (adds the row to a supplied shipment)

- [ ] **Step 1: Write the failing tests** (SHP1006 belongs to DRV006; use an inserted admin user for `created_by`)

```python
def test_unconfirmed_is_hidden_from_every_driver_read(db, clock):
    mark_unconfirmed(db, "SHP1006", admin_id)
    assert "SHP1006" not in open_shipments_for_driver(db, "DRV006")
    with pytest.raises(AuthError, match="FOREIGN_SHIPMENT"):
        authorize_shipment(db, driver_context(db, "DRV006"), "SHP1006")
    # likewise: ConversationMemoryStore.open_thread(driver, shipment) raises FOREIGN_SHIPMENT,
    # _list_my_shipments returns no SHP1006, cancel_pending_change raises FOREIGN_SHIPMENT,
    # request_change returns NOT_MOVABLE

def test_a_shipment_without_a_row_behaves_as_before(db): ...   # SHP1006 listed and authorised
def test_a_confirmed_row_is_visible(db): ...                   # set confirmed_at, SHP1006 listed
```

Write one assertion per entry point listed under Files, including the partner-tool availability and `handle_new_position` (no message is written).

- [ ] **Step 2: Run** `... -m pytest tests/test_shipment_visibility.py` — FAIL (no such table).
- [ ] **Step 3: Add the migration and point each listed driver-facing read at `v_driver_shipments`.** Warehouse reads (`board.py`, `auth/*`) keep `shipments`.
- [ ] **Step 4: Run** the new test, then the whole suite — PASS.
- [ ] **Step 5: Commit** `feat: hide unconfirmed shipments from drivers`

### Task 2: Order number parsing

**Files:** Create `src/setuhaul/order_numbers.py`, `tests/test_order_numbers.py`

**Interfaces — Produces:**
- `normalise(ref: str) -> str` — upper case, spaces and hyphens removed
- `is_valid(ref: str) -> bool`
- `mentions(text: str) -> list[frozenset[str]]` — one set of normalised candidates per mention

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("text", ["ORD-261127-001", "ord 261127 001", "my order is Ord261127001.", "ORD-261127-001 5 pm"])
def test_finds_the_number(text):
    assert any("ORD261127001" in m for m in mentions(text))

@pytest.mark.parametrize("text", ["261127001", "call +919811100001", "ETA 1730", "ORD-12345", "WORD261127001", "ORD261127001X"])
def test_not_a_mention(text):
    assert mentions(text) == []

def test_a_shorter_number_does_not_match_a_longer_hyphenated_one():
    assert not any("ORD261127" in m for m in mentions("ORD-261127-001"))

def test_validity():
    assert is_valid("ORD-261127-001") and not is_valid("ORD-DEMO-P1") and not is_valid("ORD-261127-001 ok")
```

- [ ] **Step 2: Run** — FAIL (module missing).
- [ ] **Step 3: Implement.** Hyphen-joined digits are one unit; spaces separate units, and each cumulative run of units is a candidate (at least six digits). ASCII digits only.

```python
_MENTION = re.compile(r"(?<![A-Za-z0-9])ORD[ \t-]*([0-9]+(?:-[0-9]+)*(?:[ \t]+[0-9]+(?:-[0-9]+)*)*)(?![A-Za-z0-9])", re.IGNORECASE)

def mentions(text: str) -> list[frozenset[str]]:
    found = []
    for match in _MENTION.finditer(text):
        run, candidates = "", set()
        for unit in match.group(1).split():
            run += unit.replace("-", "")
            if len(run) >= 6:
                candidates.add("ORD" + run)
        if candidates:
            found.append(frozenset(candidates))
    return found
```

- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: parse order numbers`

### Task 3: Confirmation in the message pipeline

**Files:**
- Create: `src/setuhaul/order_check.py`, `tests/test_order_check.py`
- Modify: `escalation.py` (`REASON_CODES` + `ORDER_CONFIRMATION_LOCKED`, and any per-reason table it feeds), `language.py`, `api.py` (`_process_trusted`), `conversation.py` (`_facts`, `_finalize`)

**Interfaces:**
- Consumes: `order_numbers.mentions`, `order_numbers.normalise`
- Produces: `MAX_ATTEMPTS = 5`
- Produces: `has_waiting(conn, driver_id: str) -> bool`
- Produces: `check(conn, clock: Clock, *, driver_id: str, thread_id: str, text: str, lang: str, sink) -> OrderReply | None`, with `OrderReply(kind: Literal["CONFIRMED","MISMATCH","LOCKED"], text: str, case_id: str | None)`
- Wording keys: `order_confirmed` (`order`, `facility`, `when`), `order_mismatch`, `order_locked`, `order_waiting`

English copy:
- `order_confirmed`: "Order {order} is confirmed. Deliver to {facility}, planned {when}. How can I help?"
- `order_mismatch`: "That order number does not match. Please check it and send it again."
- `order_locked`: "I cannot confirm that order. Please contact your warehouse."
- `order_waiting`: "A shipment is waiting for you. Send its order number to confirm it."

Rules, in order, when `check` runs:
1. No waiting shipment (unconfirmed and not `CANCELLED`) or no mention: return `None`.
2. A mention matches waiting, unlocked shipments: confirm every match → `CONFIRMED`.
3. A mention matches a waiting, locked shipment → `LOCKED`.
4. Every mention matches an already confirmed shipment of this driver: `None`.
5. All waiting shipments locked → `LOCKED`.
6. Otherwise add one attempt to each unlocked waiting shipment. At five, set `locked_at`, open one case naming a locked shipment → `LOCKED`; else `MISMATCH`.

- [ ] **Step 1: Write the failing tests**, posting through the real route with a gateway that records calls

```python
def test_the_right_number_confirms_without_calling_the_model():
    body = send("ORD-261127-001")
    assert "ORD-261127-001" in body["reply"] and gateway.calls == []
    assert confirmed_at("SHP-T1") is not None
    assert chat_texts(driver)[-2:] == ["ORD-261127-001", body["reply"]]

def test_fifth_wrong_number_locks_and_opens_one_case():
    for _ in range(4):
        assert send("ORD-999999-999")["reply"] == say("order_mismatch", "en")
    assert send("ORD-999999-999")["reply"] == say("order_locked", "en")
    assert cases("ORDER_CONFIRMATION_LOCKED") == 1
    assert send("ORD-261127-001")["reply"] == say("order_locked", "en")
    assert confirmed_at("SHP-T1") is None

def test_a_replayed_message_counts_once(): ...          # same message_id twice, failed_attempts == 1
def test_another_drivers_number_reads_like_any_wrong_one(): ...
def test_other_text_reaches_the_model_with_no_details():
    body = send("my truck broke down")
    assert body["reply"].endswith(say("order_waiting", "en"))
    prompt = gateway.calls[0][0][0].content
    assert "shipment_waiting_for_order_number" in prompt
    assert all(x not in prompt for x in ("SHP-T1", "ORD-261127-001"))
def test_digits_without_ORD_are_not_an_attempt(): ...   # "261127001": failed_attempts == 0, prompt appended
def test_no_waiting_shipment_changes_nothing(): ...     # no new fact key, no appended line
```

Also: two matching numbers in one message confirm both; the reply is in Hindi for Devanagari text containing the number.

- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement `order_check`**, in one `immediate()` transaction; record the case through `sink` after it commits.
- [ ] **Step 4: Wire it.** In `_process_trusted`, after `thread_id` is chosen: language from `detect_language` (fall back to `en`). On a result, append the DRIVER and AGENT messages with `memory.append`, store and return the body `{"status": "REPLIED", "reply": ..., "language": ..., "reason_code": None, "case_id": ...}` through the existing DONE path, without building a gateway.
- [ ] **Step 5: Add the prompt fact and line.** `_facts` adds `"shipment_waiting_for_order_number": True` only when `has_waiting`; `_finalize` then appends `say("order_waiting", lang)` as the last part. Leaving the key out otherwise keeps recorded traces unchanged.
- [ ] **Step 6: Run** the new tests and whole suite — PASS.
- [ ] **Step 7: Commit** `feat: confirm shipments by order number`

### Task 4: Open availability and create a shipment

**Files:** Create `src/setuhaul/auth/shipments.py`, `tests/test_shipment_create.py`

**Interfaces — Produces** (pydantic models with `extra="forbid"`; refusals raise `AuthError(code)`):
- `Availability(facility_id, first_date: date, last_date: date, dock_ids: list[str], from_time: time, to_time: time, slot_minutes: int = 60, crews: int, equipment_available: bool = True)`
- `open_availability(conn, *, actor: User, data: Availability, clock) -> AvailabilityResult(slots_created: int, days_unchanged: list[str])`
- `NewShipment(order_number, driver_id, vehicle_id, origin_name, origin_city, facility_id, customer_name, product_category, load_weight_kg: int, pallet_count: int | None, required_dock_type, temperature_control_required: bool, priority_code, expected_unload_min: int, planned_departure: datetime, slot_id, latest_arrival: datetime | None, penalty_applies: bool)`
- `create_shipment(conn, *, actor: User, data: NewShipment, clock) -> str` — id `SHP-` + 8 hex characters
- Codes: `FACILITY_NOT_FOUND`, `PARTNER_FACILITY`, `DOCK_NOT_FOUND`, `INVALID_DATES`, `INVALID_TIMES`, `BAD_ORDER_NUMBER`, `DUPLICATE_ORDER_NUMBER`, `DRIVER_NOT_FOUND`, `DRIVER_NOT_ELIGIBLE`, `VEHICLE_CARRIER_MISMATCH`, `SLOT_NOT_AVAILABLE`, `LOAD_DOES_NOT_FIT`, `INVALID_DEPARTURE`

Availability: dates from today, at most 14 inclusive; times within facility hours; one `labour_windows` row per day (`from`–`to`); a day that already has a labour window gets no new one and is listed in `days_unchanged`; existing or overlapping dock slots are skipped.

Creation: the driver must be in `drivers_in_scope` and `driver_eligible`; carrier comes from the driver. Insert shipment (`current_status='ASSIGNED'`, `original_eta_ts` = `latest_eta_ts` = slot start), `CONFIRMED` current appointment, `consignee_commitments`, `shipment_assignments`. Then, inside the same transaction, judge the chosen slot with `build_facility_state` and `evaluate_slot` (release time = slot start): `INFEASIBLE` or `INCOMPLETE_FEASIBILITY_DATA` raises `LOAD_DOES_NOT_FIT` and rolls back; `REQUIRES_APPROVAL` is allowed.

- [ ] **Step 1: Write the failing tests** (world from `tests.auth_helpers.build_world`, clock at `2026-11-27T10:00:00+05:30`)

```python
def test_availability_creates_slots_and_labour():
    r = open_av(first="2026-11-28", last="2026-11-29", docks=["DOCK-JAI-D1"], from_="09:00", to="13:00", crews=4)
    assert r.slots_created == 8 and count("labour_windows", "source='WAREHOUSE_UI'") == 2

def test_repeating_availability_adds_nothing():
    again = open_av(...same...)
    assert again.slots_created == 0 and again.days_unchanged == ["2026-11-28", "2026-11-29"]

def test_create_writes_every_row_and_waits_for_the_driver(): ...
def test_same_slot_twice_is_refused_and_leaves_nothing():
    create(order="ORD-261127-001", slot=s)
    with pytest.raises(AuthError, match="SLOT_NOT_AVAILABLE"):
        create(order="ORD-261127-002", slot=s)
    assert count("shipments", "order_reference='ORD-261127-002'") == 0
```

One test per code above, including: `ord 261127 001` is `DUPLICATE_ORDER_NUMBER` after `ORD-261127-001`; `wh_a` gets `FACILITY_NOT_FOUND` for Gurugram; a 15-day range is `INVALID_DATES`; a 24,000 kg load on D1 (limit 20,000) is `LOAD_DOES_NOT_FIT` with no rows left.

- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.** Audit actions `AVAILABILITY_OPENED`, `SHIPMENT_CREATED`.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: open availability and create shipments`

### Task 5: List, change driver, cancel, unlock

**Files:** Modify `src/setuhaul/auth/shipments.py`; create `tests/test_shipment_manage.py`

**Interfaces — Produces:**
- `list_shipments(conn, user: User) -> list[dict]` — app-created only, facility-scoped; keys `shipment_id, order_reference, driver_id, driver_name, facility_id, planned_start, state` where `state` is `WAITING`, `CONFIRMED`, `LOCKED` or `CANCELLED`
- `change_driver(conn, *, actor, shipment_id, driver_id, vehicle_id, clock) -> None` — resets attempts and lock
- `cancel_shipment(conn, *, actor, shipment_id, clock) -> None` — shipment `CANCELLED`, its appointment `CANCELLED`, `is_current=0`
- `unlock(conn, *, actor, shipment_id, clock) -> None`
- Codes: `SHIPMENT_NOT_FOUND`, `ALREADY_CONFIRMED`, `NOT_LOCKED`

- [ ] **Step 1: Write the failing tests**: scope (`wh_b` sees none of Jaipur's and gets `SHIPMENT_NOT_FOUND`); change and cancel after confirmation raise `ALREADY_CONFIRMED`; after `change_driver` the old driver's `order_check.check` confirms nothing and the new driver's confirms; after cancel the number confirms nothing and the slot can be used again; unlock resets `failed_attempts` to 0; a supplied shipment is `SHIPMENT_NOT_FOUND`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.** Audit actions `SHIPMENT_DRIVER_CHANGED`, `SHIPMENT_CANCELLED`, `SHIPMENT_UNLOCKED`. Cancelling appends an appointment event with `events.append_appointment_event`.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: manage assigned shipments`

### Task 6: Routes and the Shipments tab

**Files:**
- Create: `src/setuhaul/auth/shipment_routes.py`, `tests/test_shipment_routes.py`
- Modify: `api.py` (register beside `register_warehouse_routes`), `auth/pages.py` (`render_warehouse`), `auth/assets/warehouse.js`, `tests/test_auth_role_matrix.py`

**Interfaces — Produces:** `register_shipment_routes(app, deps) -> None` with `GET /v1/warehouse/shipments`, `POST /v1/warehouse/availability`, `POST /v1/warehouse/shipments`, `POST /v1/warehouse/shipments/{shipment_id}/driver|cancel|unlock`.

Status codes: `*_NOT_FOUND` 404; `DUPLICATE_ORDER_NUMBER`, `SLOT_NOT_AVAILABLE`, `ALREADY_CONFIRMED`, `NOT_LOCKED` 409; other refusals 422.

- [ ] **Step 1: Write the failing tests**: 401 without a session, 403 for `wh_none`; creation returns 201 with `shipment_id` when values arrive as strings, as `common.js` sends them; cross-site POST is 403; the page contains `data-tab="shipments"`, both forms, the three row states, and escapes a customer name containing `<script>`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement routes**, following `with_conn` in `warehouse_routes.py`.
- [ ] **Step 4: Add the tab** between Projects and Drivers: "Open availability" form (`dock_ids` multi-select), "New shipment" form (`datetime-local` inputs; slot list of future, open, unoccupied slots in scope labelled facility, dock, date, time), and the list with Change driver, Cancel (confirm prompt) and Unlock. In `warehouse.js`, choosing a facility limits the dock and slot options to it, and choosing a driver limits vehicles to their carrier.
- [ ] **Step 5: Run** the new tests and whole suite — PASS.
- [ ] **Step 6: Commit** `feat: add the Shipments tab`

### Task 7: End to end on today's date, and documents

**Files:** Create `tests/test_shipment_end_to_end.py`; modify `docs/writable-data-contract.md`, `docs/decision-register.md` (D100), `README.md`, `docs/deployment-hostinger.md` (Limits)

- [ ] **Step 1: Write the test.** Clock `2026-11-27T10:00:00+05:30`, `StubGateway`, messages through the real route: open availability for 2026-11-28, create with a 12:00 slot, send the order number, send "I am running late by 60 min", reply "Yes". Assert a live hold after the delay message and, after "Yes", the current appointment is `CONFIRMED`, starts at or after 13:00 and has `booking_source='DRIVER_CHAT'`.
- [ ] **Step 2: Run it.** If it fails, find the blocking rule or record with superpowers:systematic-debugging. If the fix needs behaviour the spec does not describe, stop and ask.
- [ ] **Step 3: Update the documents** with the new writes and decision D100, and the how-to: open availability, create, send the number.
- [ ] **Step 4: Run** `py -3.12 scripts/ci.py` — ruff, mypy and pytest pass.
- [ ] **Step 5: Check by eye** in the browser on a local copy of the database: both forms and the list.
- [ ] **Step 6: Commit** `docs: record shipment assignment`

Deployment is not part of this plan. Production runs migration 016 on start; take the usual backup first.
