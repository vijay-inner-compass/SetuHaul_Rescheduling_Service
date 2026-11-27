# 4. Holds, confirmation and cancellation

Files: `booking.py`, `cancellation.py`, `events.py`, `notifications.py`, `integrity.py`, `jobs/expire_holds.py`,
`jobs/deliver_notifications.py`.

## The design in one sentence

**A hold is an `appointments` row with status `HELD`, in the same unique index as a booking.** Migration 002 makes the
index cover `PENDING_CONFIRMATION`, `CONFIRMED`, `IN_PROGRESS` **and `HELD`**. A planner, a scheduling tool, another driver or
a bug in this code all collide on the same database constraint. `appointment_hold_meta` only adds owner, menu and expiry.
It is not the lock.

## Acquiring a hold (`acquire_hold`)

All inside one `BEGIN IMMEDIATE` transaction (the write lock is taken first, so racers queue):

1. Load the stored option set (proves ownership) and pick the chosen rank. A rank never offered is `INVALID_OPTION`.
2. **Lazy expiry:** clear any overdue hold on the same shipment or slot, so a lapsed hold never blocks a new one.
3. Option set older than 15 minutes: `OPTION_EXPIRED`.
4. Shipment already holds this same slot: return that hold (idempotent). A different slot: `SHIPMENT_ALREADY_HELD`.
5. Re-judge the option against the facility **as it is right now**. Taken by someone else: `SLOT_CONFLICT`. Any other
   change: `OPTION_CHANGED`.
6. Insert the HELD row and its metadata, append a `HOLD_ACQUIRED` event, commit.
7. `IntegrityError` from the unique index (a stale read lost a race): `SLOT_CONFLICT`. A busy database: `LOCK_TIMEOUT`.
   Anything else: `SYSTEM_ERROR`.

Outcome classes keep failures honest: `SUCCESS`, `BUSINESS_CONFLICT` (a normal "no"), `LOCK_TIMEOUT` (retry later, nothing
changed) and `SYSTEM_ERROR`. **A lock timeout is never reported as a conflict or a success.**

## Confirming (`confirm_reschedule`)

One transaction does all of this, or none of it:

1. Idempotency check on (driver, thread, message, action). Same message again: replay the stored answer. Same id with
   different parameters: `IDEMPOTENCY_MISMATCH`.
2. The hold must exist and belong to this driver, thread and shipment (`FOREIGN_HOLD`).
3. Already confirmed: `ALREADY_CONFIRMED`. No longer HELD: `HOLD_EXPIRED` or `HOLD_NOT_ACTIVE`. Past its expiry: expire it,
   `HOLD_EXPIRED`.
4. **Re-evaluate feasibility.** If the slot is no longer feasible the hold is released and the original is kept
   (`FEASIBILITY_CHANGED` or `APPROVAL_REQUIRED`).
5. If unloading has already started (`IN_PROGRESS`), the original cannot move (`ORIGINAL_NOT_RESCHEDULABLE`).
6. Only now: end the original, promote the hold to `CONFIRMED`/current, stamp the metadata, append history, queue
   notices to planner, consignee and operations, store the idempotent answer.

Test hooks (`_fault`) can force a crash at each named step. `test_failure_at_any_write_point_rolls_everything_back` proves
that a failure anywhere leaves the original appointment intact and nothing half-done.

## Cancelling (`cancellation.py`)

* **"Never mind"** (`cancel_pending_change`): releases the hold. The original appointment is never touched.
* **Cancel a confirmed appointment** is automatic only if all of these hold: not already in progress, more than 120 minutes
  before the slot, and a known customer commitment with no penalty and not exceptional. Otherwise it **escalates and
  changes nothing**: `CANCELLATION_CUTOFF`, `CUSTOMER_COMMITMENT`, or `CUSTOMER_COMMITMENT_UNKNOWN`. A pending hold for the same
  shipment is released with it.
* **Return to the original slot** (`request_original_slot_again`) is a *new request*: the old slot may have been given away,
  so it is re-judged and offered like any option, never restored.

## History, outbox and delivery

* `events.append_appointment_event` writes immutable history (triggers abort UPDATE and DELETE).
* `notifications.enqueue_downstream` queues three notices (`WAREHOUSE_PLANNER`, `CONSIGNEE`, `OPERATIONS`) inside the booking transaction. No outbound transport is connected for those three audiences in the deployed service yet, so their rows stay `PENDING` in `notification_outbox`; only `DRIVER` notices are sent in production (via Telegram by `DriverNoticeRunner`). In tests and the demo console, delivery runs **outside** the booking transaction
  (`deliver_pending_notifications`): at least once, with a stable `dedupe_key`. A delivery failure never touches the
  appointment. A transient failure retries up to 3 attempts; a permanent one is never retried.

## Expiry (`jobs/expire_holds.py`)

`expire_due_holds` expires holds whose time has come (inclusive: `expires_epoch <= now`) in batches, one transaction each,
re-checking status so duplicates are no-ops. `MaintenanceRunner` runs it in a background thread with its own connection, at
start-up and every 30 seconds, and purges old menus once a day.

## Integrity (`integrity.py`)

`check_invariants` runs 12 queries that must each return no rows (no duplicate active slot, no duplicate current appointment,
one hold per shipment, metadata always matching state, history always having a parent, cases consistent, a replaced
appointment never left active) plus SQLite's foreign-key check. The concurrency proof runs it after every action.

## Edge cases and failure scenarios

| Situation | Result | Test |
|---|---|---|
| Ten threads, one slot | exactly one winner | `test_ten_synchronized_threads_one_slot_exactly_one_winner` |
| Six processes, one slot | exactly one winner | `test_six_worker_processes_one_slot_exactly_one_winner` |
| Planner inserts into a held slot | database rejects it | `test_external_writer_hits_the_same_uniqueness_constraint` |
| Same "take option 1" twice | same hold returned | `test_repeating_the_same_hold_request_returns_the_same_hold` |
| Second slot for one shipment | refused | `test_one_shipment_cannot_hold_two_slots` |
| Option set older than 15 minutes | `OPTION_EXPIRED` | `test_expired_option_set_is_refused` |
| Slot changed since shown | `OPTION_CHANGED` | `test_option_that_changed_since_it_was_shown_is_not_held` |
| Confirm at exactly the expiry second | hold has lapsed | `test_exactly_at_expiry_the_hold_lapses` |
| Confirm an expired hold | refused, original stands | `test_expired_hold_cannot_be_confirmed_and_original_stands` |
| Sweep runs twice | no double expiry | `test_duplicate_sweep_is_idempotent` |
| Next driver arrives before the sweep | lazy expiry lets them in | `test_lazy_expiry_lets_the_next_driver_in_before_any_sweep` |
| Failure at any write point of confirm | everything rolls back | `test_failure_at_any_write_point_rolls_everything_back` |
| Reply lost after commit, driver retries | stored answer replayed | `test_response_loss_after_commit_is_recoverable_by_retry` |
| Same message id, different hold | mismatch | `test_same_key_with_different_parameters_is_a_mismatch` |
| Another driver confirms my hold | refused | `test_only_the_owner_of_the_hold_can_confirm` |
| Original already in progress | never ended | `test_an_original_already_in_progress_is_never_ended` |
| Slot lost before confirm | hold released | `test_feasibility_is_rechecked_and_a_lost_slot_releases_the_hold` |
| Database busy | `LOCK_TIMEOUT`, not a conflict | `test_lock_timeout_is_not_a_business_conflict` |
| Cancel exactly 120 minutes out | allowed | `test_cancel_exactly_at_cutoff_is_allowed_and_one_minute_inside_escalates` |
| Cancel with unknown customer commitment | escalates | `test_unknown_customer_commitment_is_not_assumed_harmless` |
| Notice delivery fails | appointment untouched | `test_notification_failure_never_rolls_back_the_appointment` |
| Duplicate delivery after a crash | absorbed by the dedupe key | `test_duplicate_delivery_after_a_crash_is_absorbed_by_the_dedupe_key` |
| Random sequences of every action | invariants hold after every step | `test_machine_actually_exercises_every_kind_of_action`, `scripts/run_integrity_proof.py` |
