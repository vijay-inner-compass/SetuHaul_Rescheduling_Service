# 3. ETA, feasibility and options

Files: `eta.py`, `capacity.py`, `feasibility.py`, `allocation.py`, `option_history.py`, `repositories.py`.

## ETA (`eta.py`): "when will the truck really arrive?"

`derive_eta` returns exactly one of three things:

| Outcome | When |
|---|---|
| `EtaEvidence` | an arrival time with a source and confidence |
| `ClarificationNeeded` | a fact is missing; carries the question to ask |
| `EtaConflict` | the driver and the gate record contradict each other |

Rules, in order:

1. **Gate check-in exists.** With no delay report, the gate time is the arrival (high confidence). *With* a delay report it
   is a contradiction (`CONTRADICTORY_ARRIVAL_STATE`), so a human decides.
2. No report at all: ask "How late are you...?".
3. Explicit time: used as declared.
4. Repair: needs travel time. Without it, `MISSING_TRAVEL_TIME`. ETA = start + repair + drive.
5. "N minutes late": measured from the **planned** ETA (decision D6) unless the driver names the current ETA as the
   baseline. If the named baseline does not exist, ask.

## Capacity (`capacity.py`): a snapshot of one facility

`build_facility_state` loads rules, docks, slots, dock events, occupancy and labour into memory so slot evaluation is pure.

* **Expired holds do not occupy capacity** in the occupancy query, whether or not the sweep has run.
* `effective_events` closes an open-ended blockage when a later `REOPENED` event exists.
* `slot_version` is a hash of every fact that decides whether a slot is offerable (slot, dock, overlapping events,
  occupant). If it changes between "options shown" and "hold requested", the option is stale.

## Feasibility (`feasibility.py`): can this slot take this truck?

`evaluate_slot` returns one of four statuses:

| Status | Meaning |
|---|---|
| `FEASIBLE` | can be offered |
| `INFEASIBLE` | a hard fact rules it out; **all** failing reasons are listed, not just the first |
| `REQUIRES_APPROVAL` | consignee deadline would be missed and a penalty applies or the delivery is exceptional |
| `INCOMPLETE_FEASIBILITY_DATA` | evidence is missing (labour, customer commitment, or the snapshot is older than 5 minutes) |

Hard reasons include: own current slot, slot in the past, slot blocked, dock out of service, dock event overlap, slot
held by someone else, outside facility hours, after the last new start time, dock type mismatch, refrigeration needed,
heavy load needs heavy dock, weight over the dock limit, unloading longer than the slot, truck cannot arrive in time,
and the driver's own earliest/latest times.

**The central principle:** missing evidence is never replaced by an assumption. No labour rows means
`INCOMPLETE_FEASIBILITY_DATA`, not "assume crews are free". Hard infeasibility is reported ahead of missing data.

`search_feasible_slots` judges every slot, sorts deterministically (start, then dock code), and picks an overall status in
priority order: `OK`, then `REQUIRES_APPROVAL`, then `INCOMPLETE_FEASIBILITY_DATA`, then `NO_SLOT`.

## Ranking and allocation (`allocation.py`)

* `rank_options` keeps only feasible slots, sorts by start, dock code, slot id (so ties never vary between runs) and numbers
  the first three from 1.
* Policy (D5): **first come, first served at successful hold commit.** Priority never pre-empts an existing hold.
  `allocate_fcfs` is the pure statement of the rule used for policy tests; the real enforcement is the database index in
  `booking.acquire_hold`.

## Option history (`option_history.py`): remembering what the driver saw

* `record_option_set` stores the options, their context and every line's fingerprints. The thread and shipment must belong to
  the driver (`FOREIGN_THREAD` / `FOREIGN_SHIPMENT`). An option set lives 15 minutes.
* `get_option_set` requires driver, thread **and** shipment to match; anything else looks like "does not exist".
* `diff_option_set` re-judges each stored option against live state and classifies it: `UNCHANGED`,
  `CHANGED_STILL_FEASIBLE`, `DISAPPEARED`, `NEWLY_BLOCKED`, `NEWLY_HELD`, `NEWLY_OCCUPIED`, `NEWLY_INCOMPATIBLE`,
  `NO_LONGER_FEASIBLE`, `DATA_NOW_INCOMPLETE`, with a plain-language explanation.
* The driver is not shown the list: the first option is held, and "No" moves to the next (D93).
* Option sets older than 30 days are purged by the maintenance job.

## `repositories.py`

Read-only audit queries (duplicate active slots, orphan appointments, overlapping slots, naive timestamps ...) and a
`shipment_trace` that gathers everything known about one shipment. Used by `scripts/audit_data.py`.

## Edge cases and failure scenarios

| Situation | Result | Test |
|---|---|---|
| Driver says "repair 90 min", no drive time | asks for the drive time, nothing else happens | `test_repair_duration_without_travel_needs_clarification` |
| Driver says "late" but the gate shows arrival | contradiction, hand-off | `test_driver_claims_away_after_gate_in_is_contradictory` |
| Negative delay | invalid | `test_negative_delay_is_invalid` |
| Naive timestamp in input | rejected at the schema boundary | `test_naive_timestamps_rejected_at_schema_boundary` |
| Load weight exactly equal to dock limit | allowed | `test_load_exactly_at_dock_limit_is_allowed` |
| Load heavier than the dock | rejected | `test_load_heavier_than_dock_limit_rejected` |
| No labour or customer data | `INCOMPLETE`, never assumed | `test_missing_labour_and_consignee_data_is_incomplete_not_feasible` |
| Hard infeasibility plus missing data | infeasible wins | `test_hard_infeasibility_wins_over_missing_data` |
| Snapshot older than 5 minutes | incomplete | `test_stale_facility_state_is_incomplete` |
| Dock breakdown then reopening | block closes at the reopen time | `test_reopened_event_closes_an_open_ended_block` |
| Penalty or exceptional customer | requires approval | `test_penalty_commitment_requires_approval`, `test_exceptional_commitment_requires_approval` |
| Deadline missed, no penalty | feasible with a warning | `test_missed_deadline_without_penalty_is_a_warning_not_a_block` |
| Slot taken after it was offered | explained as newly occupied | `test_option_taken_by_someone_else_is_explained` |
| Offer reviewed after rules changed | flagged | `test_changed_facility_rules_are_flagged` |
| Another driver's option set id | refused | `test_option_set_cannot_be_selected_by_another_driver_thread_or_shipment` |
| Two critical loads with no single slot | no option; hand-off | `test_snapshot_reality_two_critical_loads_have_no_single_slot_option` |
| 360-load search speed | p95 under 500 ms | `test_capacity_search_p95_under_500ms` |
