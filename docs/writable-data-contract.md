# Writable Data Contract (Phase 1 boundaries)

The audit and all Phase 1 code are read-only. The table below names what the prototype **may** write in later phases. Exact column-level writes are added to this file in Phase 6, per transaction.

## Supplied tables the prototype may write (after schema verification in the owning phase)
| Table | Write | Phase |
|---|---|---|
| `appointments` | insert held and replacement rows; status transitions; set `is_current`, `replaced_appointment_id`, `updated_at`. Requires a table rebuild to add `HELD`. | 5, 6 |
| `driver_exceptions` | insert and update exception status | 8, 9 |
| `eta_updates` | insert `DRIVER_DECLARED` rows with baseline recorded in `note` or an extension column | 2, 8 |
| `chat_threads`, `chat_messages` | insert driver and agent turns | 3, 8 |
| `drivers`, `vehicles`, `carriers` | **insert-only** (auth/roles feature, migration 011): rows supplied with the data package are never deleted, and are updated only by driver delete and restore (`drivers.driver_status`, see below). Only rows created through the app may have `drivers.driver_status` set, at approval. | auth |
| `operational_messages` | not written directly; downstream notices go through the new outbox | 6 |

## Supplied tables that are read-only
One exception (decision D93): `shipments.destination_facility_id` may be updated, only when a facility change request is approved; the original is kept in `facility_change_requests` (migration 013). No other shipment column is written, except as the next paragraph says.

Shipments tab (decision D100, migration 016): the Warehouse screen may insert into `shipments`, `appointment_slots`, `appointments` (`booking_source` `WAREHOUSE`), `labour_windows` and `consignee_commitments` (both `source` `WAREHOUSE_UI`). Only for a shipment it created and the driver has not confirmed, it may change `driver_id`, `vehicle_id`, `carrier_id`, or cancel the shipment and its appointment. Supplied rows are never changed.

Dock Access Pass (decision D101): for shipments created on that tab, the app may set `shipments.current_status` (`AT_GATE` at check-in, `COMPLETED` at check-out), insert or update `facility_checkins` (`gate_in_ts`, `arrival_state`, `gate_out_ts`), and set the appointment `COMPLETED` at unloading complete.

Otherwise these supplied tables are read-only (the exceptions are listed below):

`shipments`, `vehicle_types`, `facilities`, `docks`, `dock_status_events`, `appointment_slots`, `facility_rules`, `facility_contacts`, `facility_checkins`.

## New extension tables (named separately)
`option_sets`, `option_set_items` (Phase 4, migration 001); `labour_windows`, `consignee_commitments` (Phase 4, migration 006, read by feasibility, written only by operations or fixtures); `appointment_hold_meta` (Phase 5, migration 002); `appointment_events`, `notification_outbox`, `maintenance_runs` (migration 003, written in the same transaction as the change they describe); `idempotency_keys`, `appointment_events`, `notification_outbox` (Phase 6); `escalation_cases`, `escalation_events` (Phase 9, migration 004, events append-only); `driver_sessions`, `channel_identities`, `conversation_state` (Phase 3, migration 005); `users`, `user_facilities`, `auth_sessions`, `admin_credentials`, `driver_profiles`, `telegram_identities`, `projects`, `project_drivers` and `audit_log` (auth/roles, migration 011; `audit_log` is append-only, enforced by triggers).

## Rules
Migrations 013 to 022 add further extension tables: facility change requests, model calls and turns, saved settings, shipment assignments, pass and visit tables, dock blocks, driver deletions, account links, chat message tags and crew numbers.

Writable connections use WAL, `foreign_keys=on` and a 5000 ms busy timeout (`setuhaul.db.open_connection`). Writes happen in `BEGIN IMMEDIATE` transactions. The seed file in `data/` is migrated in place only through numbered files in `migrations/`; the original in `SetuHaul_Data_Package/` is never touched.

## Shipments tab writes (exact)
| Operation | Tables written |
|---|---|
| `open_availability` | `appointment_slots` (missing ones only), `labour_windows` (days with none), `audit_log` |
| `create_shipment` | `shipments`, `appointments`, `consignee_commitments`, `shipment_assignments`, `audit_log` |
| `change_driver` | `shipments`, `shipment_assignments` (attempts reset), `audit_log` |
| `cancel_shipment` | `shipments`, `appointments`, `appointment_events`, `audit_log` |
| `unlock` | `shipment_assignments`, `audit_log` |
| pass check | `shipment_assignments`, `chat_messages`, `inbound_messages`; on the fifth wrong code a hand-off case |
| pass issue, delivery, revoke | `truck_visits`, `visit_shipments`, `dock_access_authorizations`, `audit_log` |
| gate check-in, unloading, check-out | the three tables above, `shipments`, `facility_checkins`, `appointments`, `appointment_events`, `audit_log` |

## Warehouse and service writes added later
| Operation | Tables written |
|---|---|
| start-up (`round_clock.open_all_hours`) | `facilities.open_time` and `close_time` (all rows, 00:00), `facility_rules.active_flag` for `LAST_NEW_START_TIME` |
| crew number and slot top-up | `facility_crews`, `appointment_slots` (missing ones only), `labour_windows` (`source` `AUTO`; crew count updated on future `AUTO` rows), `audit_log` |
| case resolved with a slot (`assign.py`) | `appointments` (old CANCELLED, new CONFIRMED, `booking_source` `WAREHOUSE`; holds released), `appointment_events`, `notification_outbox`, pass tables (window), `audit_log` |
| dock block and reopen | `dock_status_events`, `dock_blocks`, `appointment_slots.slot_status`, `appointments` and history for moved or freed bookings |
| free a slot | `appointments` (CANCELLED), history, `notification_outbox` |
| delete and restore a driver | `shipments.current_status`, `facility_change_requests`, `drivers.driver_status` (**including supplied drivers**), `telegram_identities`, `project_drivers`, `driver_deletions` |
| Operations message and resend | `chat_messages`, `chat_message_loads`, `notification_outbox`, `audit_log` |

## Phase 5 writes (exact)
| Operation | Tables written | Transaction |
|---|---|---|
| `acquire_hold` | `appointments` (insert HELD, `is_current=0`, `replaced_appointment_id` = current appointment), `appointment_hold_meta` (insert), `appointment_events` (HOLD_ACQUIRED); lazily `appointments`/`appointment_hold_meta`/`appointment_events`/`notification_outbox` for any expired hold on the same shipment or slot | one `BEGIN IMMEDIATE` |
| `release_hold` | `appointments` (HELD -> CANCELLED, reason `HOLD_RELEASED`), `appointment_hold_meta` (released_at, reason), `appointment_events` (HOLD_RELEASED) | one `BEGIN IMMEDIATE` |
| `expire_due_holds` | same as lazy expiry: `appointments`, `appointment_hold_meta`, `appointment_events` (HOLD_EXPIRED), `notification_outbox` (driver notice, deduped by `HOLD_EXPIRED:<appointment_id>`) | one transaction per hold |
| `MaintenanceRunner.run_once` | the above, plus `option_sets`/`option_set_items` (delete, once per 24 h) and `maintenance_runs` | separate transactions |

The original appointment row is never modified by any Phase 5 operation.

## Phase 6 writes (exact)
| Operation | Tables written | Notes |
|---|---|---|
| `confirm_reschedule` | `appointments` (original: CANCELLED, `is_current=0`, reason RESCHEDULED; hold row: CONFIRMED, `is_current=1`, `confirmed_at`, `replaced_appointment_id`), `appointment_hold_meta` (released, reason CONFIRMED), `appointment_events` (ORIGINAL_ENDED, RESCHEDULE_CONFIRMED), `notification_outbox` (3 rows), `idempotency_keys` | single `BEGIN IMMEDIATE`; refusal paths also write `idempotency_keys` and may release the hold |
| `cancel_pending_change` | `appointments` (hold CANCELLED), `appointment_hold_meta`, `appointment_events` (HOLD_RELEASED) | original untouched |
| `cancel_confirmed_appointment` | `appointments` (CANCELLED, `is_current=0`, reason DRIVER_CANCELLED; any hold released), `appointment_events` (APPOINTMENT_CANCELLED), `notification_outbox` (3 rows), `idempotency_keys` | escalation paths write only `idempotency_keys` |
| `request_original_slot_again` | `option_sets`, `option_set_items` | no appointment row changes |
| `deliver_pending_notifications` | `notification_outbox` (status, attempts, last_error, delivered_at) | outside any booking transaction |

`appointment_events` is append-only: triggers abort any UPDATE or DELETE.

## Phase 9 writes (exact)
| Operation | Tables written |
|---|---|
| `create_handoff` | `escalation_cases` (insert, or a `DUPLICATE_REQUEST` event only), `escalation_events`, `chat_threads.thread_status` = ESCALATED |
| `claim_case` | `escalation_cases` (OPEN -> CLAIMED), `escalation_events`, `chat_messages` (one OPERATIONS update) |
| `resolve_case` | `escalation_cases` (-> RESOLVED, resolution fields), `escalation_events`, `chat_messages` (OPERATIONS update), `chat_threads.thread_status` = OPEN |
| `cancel_case_by_driver` | `escalation_cases` (-> CANCELLED), `escalation_events`, `chat_messages`, `chat_threads` |
| `reopen_case` | `escalation_cases` (RESOLVED -> OPEN, `reopened_count`+1), `escalation_events`, `chat_threads` = ESCALATED |
| `ingest_fallback_file` | the same as `create_handoff`, `source='FALLBACK_FILE'` |
