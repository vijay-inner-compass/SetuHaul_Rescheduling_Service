# 1. Foundations

Files: `clock.py`, `config.py`, `db.py`, `schemas.py`, `migrations/001`–`008`.

## Purpose

Everything else stands on four decisions: time is injected, settings are central and frozen, every timestamp is
timezone-aware, and the schema is changed only by numbered migrations.

## `clock.py`: injectable time

* `Clock` is a one-method protocol (`now()`). `SystemClock` returns real IST time. `FakeClock` holds a fixed instant that
  tests and the demo move with `advance()` and `set()`.
* **Why:** a 180-second hold, a 20-per-minute rate limit and a 30-day purge are untestable if code reads the wall clock.
* **Edge case:** `FakeClock` refuses a naive datetime. A time without a timezone is ambiguous, so it is rejected rather
  than guessed.

## `config.py`: central settings

* `Settings` is a frozen dataclass: hold lifetime 180 s, memory 20 turns / 12,000 characters / 24 h, model caps (6 rounds,
  4 tools, 8 s a call, 30 s a message), rate limits 20 a minute and 100 an hour, cancellation cut-off 120 minutes.
* Only three values come from the environment (database path, timezone, Redis URL). Everything else is policy in code, so
  it is reviewed, not tweaked at run time.
* `escalation_sla_minutes` is `None`: no response time has been approved, so none is ever promised to a driver.

## `db.py`: connections, timestamps, migrations, transactions

* `parse_operational_timestamp` accepts only ISO-8601 with an explicit offset and returns an aware IST datetime.
  **Rejected:** empty, date-only, malformed, and naive strings. (`TimestampError`)
* `open_connection` enables WAL, foreign keys and a 5-second busy timeout. `readonly=True` uses `mode=ro`, so the audit
  and readiness probes physically cannot write.
* `apply_migrations` runs `migrations/NNN_*.sql` in order, each inside a transaction with its bookkeeping row, with
  foreign keys switched off during the script and `foreign_key_check` afterwards.
* `make_working_db` copies the seed database and migrates the copy. **The seed is never written.**
* `immediate()` is the transaction used for every write: `BEGIN IMMEDIATE` takes the single write lock up front. Two
  requests racing for one slot queue at this line, so the second sees the first one's committed result. Any exception
  rolls everything back.

## `schemas.py`: contracts

* `Aware` is a timestamp type that must carry a timezone and is converted to IST.
* `DelayReport` has three kinds (`LATE_BY`, `REPAIR`, `EXPLICIT_ETA`) and a validator that each kind carries its own
  required value.
* `EtaEvidence`, `ClarificationNeeded` and `EtaConflict` are the three possible outcomes of ETA derivation.
* Every model is frozen and `extra="forbid"`: an unknown field is an error, not silently ignored.

## Migrations

| File | Adds | Key idea |
|---|---|---|
| 001 | `option_sets`, `option_set_items` | exactly what each driver was shown, cascade-deleted with its set |
| 002 | rebuilds `appointments` with `HELD`; `appointment_hold_meta` | **the capacity lock**: one active row per slot, holds included |
| 003 | `appointment_events`, `notification_outbox`, `maintenance_runs` | immutable history (triggers abort UPDATE/DELETE); transactional outbox with a unique `dedupe_key` |
| 004 | `escalation_cases`, `escalation_events` | one active case per key; immutable case history |
| 005 | `channel_identities`, `driver_sessions`, `conversation_state` | phone → driver; session tokens stored only as a hash |
| 006 | `labour_windows`, `consignee_commitments` | empty by default so feasibility reports "cannot check", with a `source` on every row |
| 007 | `idempotency_keys` | exact-retry answers per (driver, thread, message, action) |
| 008 | `inbound_messages` | duplicate webhook delivery handling |

Migration 002 rebuilds `appointments` because SQLite cannot alter a `CHECK` constraint. Views that read the table are
dropped first and recreated after.

## What to test or show

* Naive timestamps are refused: `test_parse_rejects_naive_or_invalid`.
* The seed database is untouched: `test_database_present_and_hash_matches_supplied_seed`.
* The index covers holds: `test_unique_indexes_cover_holds_in_the_same_boundary`.
* History cannot be edited: `test_every_hold_transition_is_in_immutable_history`, `test_case_events_cannot_be_changed_or_deleted`.

See the [scenario catalogue](../scenario-catalogue.md) for the full list.
