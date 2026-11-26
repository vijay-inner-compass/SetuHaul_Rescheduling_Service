# Dock Access Pass, Stage 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Dock Access Pass authorises a truck visit: issued with the shipment, sent by the bot, redeemed once in chat, and checked by a security guard at check-in and check-out.

**Architecture:** New tables hold visits and passes; only a SHA-256 hash of the 128-bit code is stored. The pass replaces order-number confirmation in the message pipeline, which blanks codes before anything is saved or sent to a model. A new `SECURITY` role and `/gate` screen drive the visit state machine.

**Tech Stack:** Python 3.12 (`secrets`, `base64`, `hashlib`), FastAPI, SQLite migrations, server-rendered pages, Redis counters, pytest.

**Spec:** `docs/superpowers/specs/2026-11-27-dock-access-pass-stage1-design.md`

## Global Constraints

- Continue on branch `feat/shipment-assignment`. Commit the spec and this plan first. Leave `docs/ux/` alone.
- Before editing migration `016`, confirm production `/version` is not a commit containing it. If it is, put the new tables in `017` and renumber.
- Code: 128 bits from `secrets`, `DAP-` + 26 base32 characters; case, spaces, hyphens ignored; only the SHA-256 hash stored.
- The raw code never reaches the database, Redis, logs, audit, case context, model prompt or any API response. It exists in memory and in the Telegram message.
- Window: slot start − `CHECKIN_EARLY_LIMIT_MIN` (60 if unset) to slot end + 4 h. Pass times stored in UTC.
- Pass states: `ISSUED, VALIDATED, ACTIVE, COMPLETED, CLOSED, EXPIRED, REVOKED, CANCELLED`. Visit states: `EXPECTED, CHECKED_IN, OPERATIONS_COMPLETE, CHECKED_OUT, CANCELLED`. Anything not listed is refused.
- Five wrong codes in chat lock the shipment and open a case. Ten failed gate checks by one user in 15 minutes pause that user for 15 minutes.
- Gate roles: `SECURITY`, `ADMIN`. Warehouse actions: `WAREHOUSE_AUTHORITY`, `ADMIN`. All scoped by facility; out of scope reads as not found.
- Passes only for shipments created in the app. Supplied rows are never changed.
- Driver wording in `en`, `hi`, `hinglish`. Times shown in IST. `/v1/...` routes. Existing comment style.
- Tests: `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`.

## Review Focus

1. A code typed without `DAP`, with a typo, or split by spaces is still blanked from what is stored (Task 4).
2. Two guards check in the same pass at once: exactly one succeeds (Task 5).
3. A reschedule moves the window: the old time is refused and the new one accepted at the gate (Task 7).
4. Telegram is down or the driver is not linked at creation: the shipment is still created, shows "Pass not delivered", and "Send new pass" recovers it (Task 3).
5. A security user opens any warehouse, admin or coordinator route: refused (Task 1).

## File Structure

| File | Responsibility |
|---|---|
| `migrations/016_shipment_assignments.sql` | adds `truck_visits`, `visit_shipments`, `dock_access_authorizations` |
| `migrations/017_security_role.sql` | rebuilds `users` to allow `SECURITY` |
| `src/setuhaul/dock_pass.py` | code format, hashing, finding and blanking (pure) |
| `src/setuhaul/passes.py` | issue, deliver, revoke, window, expiry, cancel |
| `src/setuhaul/pass_check.py` | redemption in chat (replaces `order_check.py`) |
| `src/setuhaul/gate.py` | gate checks and visit transitions |
| `src/setuhaul/auth/gate_routes.py`, `auth/gate_pages.py`, `auth/assets/gate.js` | the gate screen |

---

### Task 1: Security role

**Files:** Create `migrations/017_security_role.sql`, `tests/test_security_role.py`. Modify `auth/users.py` (`Role`, `invite`), `auth/authz.py` (`require_role`), `auth/pages.py` (invite options, `landing`), `tests/test_auth_role_matrix.py`.

**Interfaces — Produces:** `Role = Literal["ADMIN","WAREHOUSE_AUTHORITY","SECURITY"]`. A `SECURITY` user needs facilities, exactly like a warehouse authority, and one without any is refused with `NO_FACILITIES`.

- [ ] **Step 1: Failing tests**

```python
def test_migration_keeps_existing_users(tmp_path): ...      # apply 001-016, insert users + sessions, apply 017: rows equal, FK check empty, 'SECURITY' insertable, 'GUARD' rejected
def test_admin_invites_security_with_facilities(world): ... # invite ok; without facilities -> FACILITY_REQUIRED
def test_security_is_refused_everywhere_else(world): ...    # every ROLE_GUARDED and HTML_GUARDED route in the matrix -> 403
def test_security_lands_on_the_gate(world): ...             # GET "/" -> 303 "/gate"
```

- [ ] **Step 2: Run** — FAIL (CHECK constraint).
- [ ] **Step 3: Implement.** Migration: create `users_new` with the same columns and the wider CHECK, copy, drop, rename.
- [ ] **Step 4: Run** the new tests and suite — PASS.
- [ ] **Step 5: Commit** `feat: add the security role`

### Task 2: Pass codes, tables and lifecycle

**Files:** Modify `migrations/016_shipment_assignments.sql`. Create `src/setuhaul/dock_pass.py`, `src/setuhaul/passes.py`, `tests/test_dock_pass.py`, `tests/test_passes.py`.

**Interfaces — Produces:**
- Tables: `truck_visits(truck_visit_id pk, warehouse_id, driver_id, vehicle_id, carrier_id, status, check_in_key, check_out_key, checked_in_at, completed_at, checked_out_at, created_at, row_version default 1)`; `visit_shipments(truck_visit_id, shipment_id, pk both)`; `dock_access_authorizations(authorization_id pk, token_hash unique not null, appointment_id, truck_visit_id, driver_id, vehicle_registration, carrier_id, warehouse_id, dock_id, valid_from_utc, valid_until_utc, status, issued_at, delivered_at, activated_at, revoked_at, revocation_reason, row_version default 1)`; unique index on `truck_visit_id` where status in (`ISSUED,VALIDATED,ACTIVE,COMPLETED`)
- `dock_pass.new_code() -> str`; `normalise(text) -> str`; `hash_code(code) -> str`; `find(text) -> list[str]` (normalised, including near-misses); `blank(text) -> str` (replaces each find with `DAP-[hidden]`)
- `passes.issue(conn, shipment_id, clock, actor: User | None) -> str` (raw code; creates the visit if none, revokes the live pass; call inside a transaction)
- `passes.sync_window(conn, shipment_id, clock) -> None`; `passes.revoke(conn, *, actor, shipment_id, reason, clock)`; `passes.cancel(conn, shipment_id, clock) -> None` (pass and visit `CANCELLED`); `passes.expire_due(conn, clock) -> int`; `passes.state_for(conn, shipment_id) -> sqlite3.Row | None` (latest pass)
- Codes: `SHIPMENT_NOT_FOUND`, `VISIT_IN_PROGRESS`, `NO_APPOINTMENT`, `REASON_REQUIRED`, `NO_LIVE_PASS`

- [ ] **Step 1: Failing tests**

```python
def test_code_shape_and_entropy():
    code = new_code()
    assert re.fullmatch(r"DAP-[A-Z2-7]{5}-[A-Z2-7]{5}-[A-Z2-7]{5}-[A-Z2-7]{5}-[A-Z2-7]{6}", code)
    assert len({new_code() for _ in range(1000)}) == 1000

def test_found_and_blanked_however_typed():
    code = new_code(); bare = normalise(code)[3:]
    for text in (code, code.lower(), code.replace("-", " "), f"my pass {code}.", bare, "DAP-" + bare[:-1]):
        assert find(text) and bare[:20] not in normalise(blank(text))
    assert find("I will dap in at 5") == [] and blank("ORD-261127-001") == "ORD-261127-001"
```

`test_passes.py`: issue stores only the hash (search every table's text for the code); window equals slot start − 60 min / end + 4 h in UTC, and uses `CHECKIN_EARLY_LIMIT_MIN`; a second issue leaves one live pass and the first `REVOKED`; revoke needs a reason and scope; issue during `CHECKED_IN` raises `VISIT_IN_PROGRESS`; `expire_due` expires `ISSUED` past the window and leaves `ACTIVE`.

- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.** `secrets.token_bytes(16)`, `base64.b32encode`, SHA-256 of the normalised code. `find` also catches a bare 26-character base32 run containing a digit.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: add dock access passes`

### Task 3: Issue and deliver

**Files:** Modify `auth/shipments.py` (`create_shipment`, `list_shipments`), `auth/shipment_routes.py`, `auth/shipment_pages.py`, `auth/telegram_routes.py`, `language.py`. Test `tests/test_pass_delivery.py`.

**Interfaces — Consumes:** Task 2. **Produces:**
- `create_shipment(...) -> tuple[str, str]` — `(shipment_id, code)`; the route never returns the code
- `passes.deliver(conn, client: TelegramClient | None, shipment_id, code, clock) -> bool` (sets `delivered_at`; audits `PASS_DELIVERED` or `PASS_DELIVERY_FAILED`; sends after the transaction commits)
- `passes.send_new(conn, client, *, actor: User | None, shipment_id, clock) -> bool` — issue then deliver; when `actor` is given, resets `confirmed_at`, `failed_attempts`, `locked_at`
- `passes.send_waiting(conn, client, driver_id, clock) -> None` — after a Telegram link
- Routes `POST /v1/warehouse/shipments/{id}/pass` and `.../pass/revoke` `{reason}`; list rows gain `pass_state` and `delivered: bool`
- Wording `pass_delivery` (`order`, `code`): "Your Dock Access Pass for order {order}:\n{code}\nSend it back here to confirm the shipment, and show it at the gate."

- [ ] **Step 1: Failing tests** with `FakeTelegramClient`: creating sends one message holding a code whose hash matches the live pass; the JSON response and every table hold no code; unlinked driver or a client raising `TelegramSendError` → 201, `delivered` false, page says "Pass not delivered"; linking Telegram then sends one; "Send new pass" changes the hash, resets confirmation and refuses while the truck is inside; revoke without reason is 422; other facility 404.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.** Page shows pass state, with **Send new pass** and **Revoke pass** (reason input).
- [ ] **Step 4: Run** the new tests and suite — PASS.
- [ ] **Step 5: Commit** `feat: issue and deliver passes`

### Task 4: Redeem in chat, replacing the order check

**Files:** Create `src/setuhaul/pass_check.py`, `tests/test_pass_check.py`. Delete `order_check.py`, `tests/test_order_check.py`, `order_numbers.mentions` and its tests (keep `is_valid`, `normalise`). Modify `api.py` (`_process_trusted`), `conversation.py`, `language.py`, `escalation.py` and `demo_ui.py` (code `PASS_CONFIRMATION_LOCKED` replaces the order one).

**Interfaces — Produces:** `has_waiting`, `prompt_key` (`pass_waiting` / `pass_locked`), `check(conn, clock, *, driver_id, thread_id, text, lang, sink) -> PassReply | None` with the same shape and rules as `order_check.check`. A code matches when its hash is the live pass of a waiting shipment of this driver; every other found code is one wrong attempt.

English: `pass_confirmed` (`order`, `facility`, `when`): "Pass accepted. Order {order}: deliver to {facility}, planned {when}. How can I help?"; `pass_mismatch`: "That pass is not valid. Please check it and send it again."; `pass_locked`: "I cannot accept that pass. Please contact your warehouse."; `pass_waiting`: "A shipment is waiting for you. Send its Dock Access Pass to confirm it."

- [ ] **Step 1: Failing tests** — port every case of `test_order_check.py` to codes, plus:

```python
@pytest.mark.parametrize("shape", [lambda c: c, lambda c: normalise(c)[3:], lambda c: c.replace("-", " "), lambda c: c[:-1]])
def test_the_code_is_never_kept(chat, shape):
    code = chat.issue_waiting()
    chat.send("here it is " + shape(code))
    secret = normalise(code)[3:23]
    assert secret not in chat.dump_database_normalised() and secret not in chat.dump_redis_normalised()
    assert all(secret not in normalise(p) for p in chat.model_prompts())

def test_an_order_number_alone_does_nothing(chat): ...     # shipment stays hidden and unconfirmed
def test_a_superseded_or_revoked_code_is_wrong(chat): ...
```

- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.** In `_process_trusted`, keep the raw text only for `pass_check.check`; use `dock_pass.blank(text)` for memory, `handle_message` and everything after. The idempotency fingerprint stays a hash of the raw text.
- [ ] **Step 4: Run** the suite — PASS.
- [ ] **Step 5: Commit** `feat: confirm shipments with the pass`

### Task 5: Gate operations

**Files:** Create `src/setuhaul/gate.py`, `tests/test_gate.py`. Modify `escalation.py` (`VISIT_OVERSTAY`), `demo_ui.py` (its label), `jobs/expire_holds.py` (`MaintenanceRunner.run_once` also sweeps).

**Interfaces — Produces:**
- `GateRequest(code, vehicle_registration, licence_number, request_key)`
- `validate(conn, redis, *, actor, data, clock) -> GateView(truck_visit_id, driver_name, order_reference, vehicle_registration, dock_code, valid_until)` — marks `VALIDATED`
- `check_in(conn, redis, *, actor, data, clock) -> GateView`
- `unloading_complete(conn, *, actor, truck_visit_id, clock) -> None`
- `check_out(conn, *, actor, truck_visit_id: str | None, code: str | None, request_key, clock) -> None`
- `inside(conn, actor) -> list[dict]`; `sweep(conn, clock) -> int` (expire passes; one `VISIT_OVERSTAY` case per visit still inside after `valid_until`)
- Refusals, in this order: `TOO_MANY_ATTEMPTS`, `PASS_NOT_VALID` (unknown, revoked, cancelled, closed, completed), `WRONG_WAREHOUSE`, `ALREADY_CHECKED_IN`, `TOO_EARLY`, `TOO_LATE`, `NO_APPOINTMENT`, `DRIVER_NOT_ELIGIBLE`, `DETAILS_DO_NOT_MATCH`; later `NOT_CHECKED_IN`, `UNLOADING_NOT_COMPLETE`, `VISIT_NOT_FOUND`

Writes: check-in sets pass `ACTIVE`, visit `CHECKED_IN`, shipment `AT_GATE`, `facility_checkins.gate_in_ts` and `arrival_state` (`LATE` after slot start + `checkin_grace_min`, `EARLY` more than that before it), and `confirmed_at` if still empty. Unloading complete: pass `COMPLETED`, visit `OPERATIONS_COMPLETE`, appointment `COMPLETED`. Check-out: pass `CLOSED`, visit `CHECKED_OUT`, shipment `COMPLETED`, `gate_out_ts`. Each in one `immediate()` transaction, guarded by `row_version`.

- [ ] **Step 1: Failing tests**: one per refusal; nothing about the shipment is in a refusal; vehicle and licence compare ignoring case, spaces, hyphens; the same `request_key` repeated returns the same view and writes once, a different key gets `ALREADY_CHECKED_IN`; two threads with their own connections checking in: one success; check-out before completion refused; after check-out the code is `PASS_NOT_VALID`; eleventh failure within 15 minutes is `TOO_MANY_ATTEMPTS` even with a right code; overstay releases nothing and opens exactly one case.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.** Failure counter `setuhaul:gate:{user_id}` with a 900 s expiry. Audit `GATE_CHECK` (reason in detail), `CHECK_IN`, `UNLOADING_COMPLETE`, `CHECK_OUT`.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: add gate check-in and check-out`

### Task 6: Gate routes and screens

**Files:** Create `auth/gate_routes.py`, `auth/gate_pages.py`, `auth/assets/gate.js`, `tests/test_gate_routes.py`. Modify `api.py`, `auth/pages.py` (`ASSET_TYPES`, `/gate`), `auth/shipment_pages.py` (**Unloading complete** for trucks inside), `tests/test_auth_role_matrix.py`.

**Interfaces — Produces:** `POST /v1/gate/validate`, `/v1/gate/check-in`, `/v1/gate/check-out`, `GET /v1/gate/visits`, `POST /v1/warehouse/visits/{truck_visit_id}/unloading-complete`, `GET /gate`. Refusals return `reason_code` and a plain `message` saying what to do; status 404 `*_NOT_FOUND`, 429 `TOO_MANY_ATTEMPTS`, 409 state conflicts, else 422.

- [ ] **Step 1: Failing tests**: role and scope per route; no response body contains the code; cross-site POST 403; the page has the three-field form with `autocomplete="off"`, a Check in button that appears only after validation, and the list of trucks inside with Check out.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.** `gate.js` generates `request_key` with `crypto.randomUUID()` per validated truck and clears the code field after each action.
- [ ] **Step 4: Run** the suite — PASS.
- [ ] **Step 5: Commit** `feat: add the gate screen`

### Task 7: Changes to a booked shipment

**Files:** Modify `booking.py` (`confirm_reschedule`), `auth/shipments.py` (`cancel_shipment`, `change_driver`), `auth/facility_routes.py` and `facility_change.py`. Test `tests/test_pass_changes.py`.

**Interfaces — Consumes:** `passes.sync_window`, `passes.cancel`, `passes.send_new` (actor `None`, so confirmation is kept).

Rules: confirmed reschedule → `sync_window` inside the same transaction. Driver cancelling the appointment changes nothing here; the gate refuses with `NO_APPOINTMENT`. Warehouse cancel → `cancel`. Change driver → visit moves to the new driver, vehicle and carrier, new pass sent. Approved or reverted facility move → visit's warehouse updated, new pass sent; a move to a partner warehouse cancels the pass.

- [ ] **Step 1: Failing tests**, one per rule, including: after a reschedule the old time is `TOO_EARLY`/`TOO_LATE` and the new one validates; the old code fails after a driver change; the driver need not resend after a facility move.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the suite — PASS.
- [ ] **Step 5: Commit** `feat: keep passes in step with changes`

### Task 8: End to end, documents and a look in the browser

**Files:** Rewrite `tests/test_shipment_end_to_end.py`. Create `docs/dock-access-pass.md`. Modify `docs/writable-data-contract.md`, `docs/decision-register.md` (D101), `README.md`, `docs/deployment-hostinger.md`.

- [ ] **Step 1: Write the test:** create; read the code from the fake bot; send it; report a delay and confirm; at the new time validate and check in as security; unloading complete; check out; the code is then refused. Assert at the end that no table holds the code.
- [ ] **Step 2: Run it**; fix what it finds with superpowers:systematic-debugging.
- [ ] **Step 3: Documents:** operating steps, the new writes, D101, risks from the spec, and rollback: back up first; to go back, deploy the previous image and restore that backup, since `017` rebuilds `users`.
- [ ] **Step 4: Run** `ruff check .`, `mypy src`, `pytest` (and `py -3.12 scripts/ci.py` if `.venv` is free).
- [ ] **Step 5: Browser check** on a local copy: gate form, refusal wording, Send new pass.
- [ ] **Step 6: Commit** `docs: record the dock access pass`

Not in this plan: deployment, parking, dock allocation on arrival, several shipments per visit, diagrams (stages 2 and 3).
