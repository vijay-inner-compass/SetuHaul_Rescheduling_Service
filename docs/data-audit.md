# Data Audit

Source: `data/setuhaul.sqlite3` (SHA-256 `C02806E6...F90700`, identical to the supplied `setuhaul_freight_operations.db`). Reproduce with `python scripts/audit_data.py`; full output in `docs/verification/phase1-audit-output.json`. The audit opens the database with `mode=ro` and a test confirms the file hash is unchanged afterwards.

## Schema map
18 tables, 4 views.

| Area | Tables / views (rows) |
|---|---|
| Parties | carriers (4), drivers (15), vehicle_types (5), vehicles (15) |
| Shipments | shipments (21), eta_updates (12), v_latest_eta, v_inbound_operational_state |
| Facility | facilities (2), docks (9), dock_status_events (3), facility_rules (6), facility_contacts (5) |
| Capacity | appointment_slots (106), appointments (20), v_slot_availability, v_current_facility_queue |
| Arrival | facility_checkins (5) |
| Conversation | chat_threads (12), chat_messages (20), driver_exceptions (10), operational_messages (5) |

Key constraints: `ux_active_appointment_per_slot` (unique slot where status is PENDING_CONFIRMATION, CONFIRMED or IN_PROGRESS); `ux_current_active_appointment_per_shipment` (unique shipment where `is_current=1` and an active status); `slot_end_ts > slot_start_ts`; `UNIQUE(dock_id, slot_start_ts, slot_end_ts)`; CHECK-constrained enums on all status columns.

## Data issues
**Ten issues, each with the query that proves it and its result: [data-issues.md](verification/data-issues.md).**
The integrity checks under "Findings" below pass, but they test only what the schema already guards. The two unique
indexes do not cover dock weight limits, unload overrun, dock events or whether the booked time can still be met.

| Issue | Rows | How the service treats it |
|---|---|---|
| Active appointment over the dock weight limit | 2 | new options are rejected (`WEIGHT_EXCEEDS_DOCK_LIMIT`); existing bookings are not flagged |
| Current appointment the latest ETA cannot meet | 6 | rejected as an option; found only when the driver writes |
| Unload longer than the slot | 4 | new options must fit one slot; existing bookings are not flagged |
| Active appointment inside a dock event | 3 | new options are blocked; existing bookings are not flagged |
| Heavy-dock rule against dock limits | needs confirming | both the rule and the dock limit are applied |
| Open shipment with no active appointment | 4 | handled case by case |
| Driver with two open shipments | 4 | each tool needs a shipment id and checks ownership; the model picks which |
| Exception without a shipment; duplicate exception | 2 | not assumed to map to a load |
| Duplicate chat message | 1 | excluded from rebuilt context |
| Operational message failed or still queued | 2 | not repaired by the service |

## Findings
| Check | Result |
|---|---|
| `pragma integrity_check` | ok |
| `pragma foreign_key_check` | no violations |
| Duplicate active appointment per slot | 0 |
| Duplicate current active appointment per shipment | 0 |
| Orphan appointments (missing shipment or slot) | 0 |
| Shipments missing weight, unload minutes, dock type or ETA | 0 |
| Active appointments for CANCELLED or COMPLETED shipments | 0 |
| Gate-in with no arrival_state | 0 |
| Overlapping slots on the same dock | 0 |
| Seed rows for SHP1006, SHP1009, SHP1012, SHP1013, SHP1014 | all present, all IN_TRANSIT |

## Timestamp contract
34 `*_ts` / `*_at` columns were inspected. Every non-null value uses one shape, `YYYY-MM-DDTHH:MM:SS+05:30`. There are no naive values and no mixed offsets. `facility_rules.effective_from` is a date only and `facilities.open_time`, `close_time` and `LAST_NEW_START_TIME` are bare `HH:MM` strings, which are local wall-clock times in `Asia/Kolkata`; they are combined with a date and the facility timezone to form aware datetimes.

Contract (`setuhaul.db.parse_operational_timestamp`): input must be ISO-8601 with an explicit offset (`Z` accepted); result is an aware datetime in Asia/Kolkata; naive, space-separated or malformed values raise `TimestampError`. Timestamps are never compared as strings. Test `test_comparison_uses_instants_not_strings` shows string order disagreeing with instant order for mixed offsets.

## Single-shipment trace (SHP1006)
shipment -> driver -> vehicle -> `v_latest_eta` -> facility -> current appointment -> slot -> dock -> check-in -> exceptions, via `Repository.shipment_trace`. SHP1006 has two TRAFFIC exceptions (EXC001 and EXC009); EXC009 is already marked DUPLICATE. Related oddities: EXC010 has no shipment_id, and MSG015 is flagged `is_duplicate`. Phase 8 must handle both rather than assume every exception maps to a shipment.

## Schema facts that shape later phases
- `appointments.appointment_status` has no `HELD` value, so Phase 5 requires a table rebuild (SQLite cannot alter a CHECK) and must recreate both partial unique indexes.
- Appointment history is not immutable: rows are updated in place and there is no events table. Phase 6 adds one.
- Phase 4 reads all but one of the feasibility inputs from the data: dock type and weight limits, facility hours, dock blocks, unload minutes and slot capacity. **No labour, equipment or consignee-commitment data exists**, so those checks must return `INCOMPLETE_FEASIBILITY_DATA`.
- `driver_exceptions.reported_delay_min` is stored alongside `declared_eta_ts` with no baseline recorded; Phase 2 adds the baseline type.
