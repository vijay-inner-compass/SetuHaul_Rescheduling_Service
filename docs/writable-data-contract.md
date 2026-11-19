# Writable Data Contract (Phase 1 boundaries)

The audit and all Phase 1 code are read-only. The table below names what the prototype **may** write in later phases. Exact column-level writes are added to this file in Phase 6, per transaction.

## Supplied tables the prototype may write (after schema verification in the owning phase)
| Table | Write | Phase |
|---|---|---|
| `appointments` | insert held and replacement rows; status transitions; set `is_current`, `replaced_appointment_id`, `updated_at`. Requires a table rebuild to add `HELD`. | 5, 6 |
| `driver_exceptions` | insert and update exception status | 8, 9 |
| `eta_updates` | insert `DRIVER_DECLARED` rows with baseline recorded in `note` or an extension column | 2, 8 |
| `chat_threads`, `chat_messages` | insert driver and agent turns | 3, 8 |
| `operational_messages` | not written directly; downstream notices go through the new outbox | 6 |

## Supplied tables that are read-only
`shipments`, `drivers`, `vehicles`, `vehicle_types`, `carriers`, `facilities`, `docks`, `dock_status_events`, `appointment_slots`, `facility_rules`, `facility_contacts`, `facility_checkins`. Slot blocking stays an operations action.

## New extension tables (named separately)
`option_sets`, `option_set_items` (Phase 4, migration 001); `labour_windows`, `consignee_commitments` (Phase 4, migration 006, read by feasibility, written only by operations or fixtures); `appointment_hold_meta` (Phase 5, migration 002); `appointment_events`, `notification_outbox`, `maintenance_runs` (migration 003, written in the same transaction as the change they describe); `idempotency_keys`, `appointment_events`, `notification_outbox` (Phase 6); `escalation_cases`, `escalation_events` (Phase 9); `driver_sessions`, `channel_identities`, `conversation_state` (Phase 3, migration 005).

## Rules
Writable connections use WAL, `foreign_keys=on` and a 5000 ms busy timeout (`setuhaul.db.open_connection`). Writes happen in `BEGIN IMMEDIATE` transactions. The seed file in `data/` is migrated in place only through numbered files in `migrations/`; the original in `SetuHaul_Data_Package/` is never touched.

## Phase 5 writes (exact)
| Operation | Tables written | Transaction |
|---|---|---|
| `acquire_hold` | `appointments` (insert HELD, `is_current=0`, `replaced_appointment_id` = current appointment), `appointment_hold_meta` (insert), `appointment_events` (HOLD_ACQUIRED); lazily `appointments`/`appointment_hold_meta`/`appointment_events`/`notification_outbox` for any expired hold on the same shipment or slot | one `BEGIN IMMEDIATE` |
| `release_hold` | `appointments` (HELD -> CANCELLED, reason `HOLD_RELEASED`), `appointment_hold_meta` (released_at, reason), `appointment_events` (HOLD_RELEASED) | one `BEGIN IMMEDIATE` |
| `expire_due_holds` | same as lazy expiry: `appointments`, `appointment_hold_meta`, `appointment_events` (HOLD_EXPIRED), `notification_outbox` (driver notice, deduped by `HOLD_EXPIRED:<appointment_id>`) | one transaction per hold |
| `MaintenanceRunner.run_once` | the above, plus `option_sets`/`option_set_items` (delete, once per 24 h) and `maintenance_runs` | separate transactions |

The original appointment row is never modified by any Phase 5 operation.
