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
`option_sets`, `option_set_items` (Phase 4); `appointment_hold_meta` (Phase 5); `idempotency_keys`, `appointment_events`, `notification_outbox` (Phase 6); `escalation_cases`, `escalation_events` (Phase 9); `driver_sessions`, `channel_identities` (Phase 3).

## Rules
Writable connections use WAL, `foreign_keys=on` and a 5000 ms busy timeout (`setuhaul.db.open_connection`). Writes happen in `BEGIN IMMEDIATE` transactions. The seed file in `data/` is migrated in place only through numbered files in `migrations/`; the original in `SetuHaul_Data_Package/` is never touched.
