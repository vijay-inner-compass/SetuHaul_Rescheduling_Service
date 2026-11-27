# Scenario catalogue: edge cases and failures

Every row is a situation someone might ask about, the expected behaviour, and the **test that proves it**. Run any one with
`python -m pytest -q -k <test_name>`. For a live view of the main flows see the [demonstration guide](demonstration-guide.md).

Legend: **Demo** = can be shown on the demo console; **Script** = shown by an evidence script; otherwise test only.

## A. The normal flow

| Situation | Expected | Test / how to see it |
|---|---|---|
| Late, one slot held, confirm in a later message | each step worded from committed state | `test_confirmation_happens_in_a_later_message_and_is_worded_from_the_row`; Demo A |
| A delay report holds the first feasible slot | held, not booked | `test_delay_report_holds_the_first_option`; Demo A |
| "No" | that hold released, the next one held | `test_no_releases_and_holds_the_next` |
| "No, that is fine" | treated as agreement | `test_no_that_is_fine_keeps_the_held_slot` |
| Whole required scenario | 23 checks pass | `test_every_check_in_the_scenario_passes`; `python scripts/run_scenario.py` |
| Confirmation swaps the appointments atomically | original ends only inside the transaction | `test_confirmation_is_one_transaction_that_swaps_the_appointments` |
| Confirmation writes history and three notices | planner, consignee, operations | `test_history_and_three_notifications_are_written_with_the_booking` |
| "Never mind" before confirming | hold released, original untouched | `test_never_mind_releases_the_hold_and_the_original_stands`; Script `show_phase6.py` |

## B. Input edge cases

| Situation | Expected | Test |
|---|---|---|
| Message of exactly 2000 characters / 2001 | accepted / 422 | `test_a_message_of_exactly_2000_characters_is_accepted_and_2001_is_refused` |
| Empty message | 422 before the model | `test_an_empty_message_is_refused_before_the_model` |
| Unknown JSON fields (`driver_id`, `thread_id`) | 422, client identity refused | `test_validly_signed_garbage_is_a_422_and_client_supplied_identity_is_refused` |
| Phone number written in different formats | one normalised form | `test_normalize_phone_to_e164` |
| Garbage phone number | `INVALID_PHONE` | `test_normalize_phone_rejects_garbage` |
| Delay of 0 minutes | planned ETA kept | `test_zero_delay_keeps_planned_eta` |
| Negative delay | invalid | `test_negative_delay_is_invalid` |
| Breakdown without drive time | asks for it, does nothing else | `test_repair_without_travel_time_asks_a_question_and_does_nothing_else` |
| Repair with a stated start time | uses the start, not now | `test_repair_with_stated_start_uses_start_not_now` |
| Naive (timezone-less) timestamps | rejected | `test_naive_timestamps_rejected_at_schema_boundary`, `test_parse_rejects_naive_or_invalid` |
| Driver has two active shipments and does not say which | asks which | `test_two_active_shipments_the_driver_is_asked_which` |
| A choice outside the offer | rejected, hold unchanged | `test_a_rank_outside_the_offer_is_rejected_and_changes_no_hold` |
| Option number never shown | `INVALID_OPTION` | `test_unknown_rank_is_rejected` |
| Driver replies with just a number | holds that stored option (a one-character quote counts only when it is the whole message) | `test_a_reply_of_just_a_number_holds_that_menu_line` |
| "3" appears inside a longer, unrelated message | not approval | `test_a_one_character_quote_inside_a_longer_message_is_not_approval` |
| Plain "yes" with the stand-in model | books the held slot | `test_the_rule_based_assistant_books_on_a_plain_yes` |
| Quote with odd spacing or case | still authorises | `test_the_real_quote_does_authorise_it_even_with_odd_spacing_and_case` |
| Driver's acceptable window reversed | invalid | `test_driver_constraints_window_must_be_ordered` |

## C. Races and concurrency

| Situation | Expected | Test / script |
|---|---|---|
| Ten threads want one slot | exactly one winner | `test_ten_synchronized_threads_one_slot_exactly_one_winner` |
| Six processes want one slot | exactly one winner | `test_six_worker_processes_one_slot_exactly_one_winner` |
| Mixed holds, confirms, cancels, sweeps | every invariant holds | `test_mixed_contention_preserves_every_invariant` |
| Planner writes into a held slot | the database refuses | `test_external_writer_hits_the_same_uniqueness_constraint`; Script `show_phase5.py` |
| Hold attempted on a slot a planner booked first | `SLOT_CONFLICT` | `test_hold_cannot_be_taken_on_a_slot_an_external_writer_booked_first` |
| Read was stale, the index is the backstop | conflict, no double hold | `test_unique_index_is_the_backstop_when_the_read_was_stale` |
| Two workers confirm the same message | one booking | `test_two_workers_confirming_the_same_message_yield_one_booking` |
| Two runners sweep the same holds | no double expiry | `test_two_runners_on_separate_connections_do_not_double_expire` |
| Two coordinators claim one case | one owner | `test_racing_coordinators_in_separate_connections_produce_one_owner`, `test_racing_claims_over_http_have_one_winner` |
| Two simultaneous duplicate deliveries | the model runs once | `test_simultaneous_duplicates_run_the_model_once` |
| Database busy | `LOCK_TIMEOUT`, never a conflict or success | `test_lock_timeouts_are_never_reported_as_conflicts_or_success`, `test_lock_timeout_is_not_a_business_conflict` |
| A new search replaces the earlier offer | the old option set cannot be held | `test_a_new_search_replaces_the_menu_a_number_refers_to` |
| Two searches in one message | one hold, from the latest search | `test_two_searches_in_one_message_leave_one_hold_from_the_latest_search` |
| Random action sequences | invariants after every step | `test_machine_actually_exercises_every_kind_of_action`; `python scripts/run_integrity_proof.py` |

## D. Time

| Situation | Expected | Test / how to see it |
|---|---|---|
| Return before the hold expires | hold kept | `test_return_before_expiry_keeps_the_hold` |
| Exactly at the expiry second | lapsed (inclusive) | `test_exactly_at_expiry_the_hold_lapses` |
| Confirm after expiry | refused, original stands | `test_expired_hold_cannot_be_confirmed_and_original_stands`; Demo B |
| Hold lapses while the driver is away | told once, no stale promise | `test_hold_that_lapsed_is_reported_instead_of_replaying_the_promise` |
| Next driver arrives before the sweep runs | lazy expiry lets them in | `test_lazy_expiry_lets_the_next_driver_in_before_any_sweep` |
| Sweep run twice | idempotent | `test_duplicate_sweep_is_idempotent` |
| Many overdue holds after a restart | batch limit respected | `test_restart_with_several_overdue_holds_and_a_batch_limit` |
| Offer older than 15 minutes | `OPTION_EXPIRED` | `test_expired_option_set_is_refused` |
| Slot start time already passed | not offered | `test_driver_cannot_book_slot_in_the_past` |
| Cancel exactly at the 120-minute cut-off, and one minute inside | allowed / escalates | `test_cancel_exactly_at_cutoff_is_allowed_and_one_minute_inside_escalates` |
| Reopen a case after 24 hours | refused | `test_reopen_inside_the_window_and_a_new_case_after_it` |
| Per-minute rate window rolls over | allowed again | `test_per_minute_window_resets` |
| Stale signed status request | refused | `test_driver_status_is_signed_fresh_and_limited_to_the_drivers_own_data` |
| Old option sets (over 30 days) | purged once a day | `test_maintenance_runner_expires_and_purges_option_sets_once_a_day` |

## E. Missing, contradictory or changed data

| Situation | Expected | Test |
|---|---|---|
| No labour or customer data | "cannot check", never assumed | `test_missing_labour_and_consignee_data_is_incomplete_not_feasible`; Script `show_phase4.py` |
| Hard infeasibility plus missing data | infeasible wins | `test_hard_infeasibility_wins_over_missing_data` |
| Stale facility snapshot | incomplete | `test_stale_facility_state_is_incomplete` |
| Driver says late but gate shows arrival | contradiction, hand-off | `test_gate_in_contradiction_hands_off` |
| No slot fits the load | hand-off, promises nothing | `test_no_slot_hand_off_is_complete_and_promises_nothing`, `test_when_nothing_fits_there_is_a_hand_off_and_no_menu` |
| Labour evidence missing | hand-off says what is missing | `test_missing_labour_evidence_hands_off_with_what_is_missing` |
| Penalty or exceptional customer | requires approval | `test_penalty_commitment_requires_approval`, `test_exceptional_commitment_requires_approval` |
| Unknown customer commitment on cancel | escalates, not assumed harmless | `test_unknown_customer_commitment_is_not_assumed_harmless` |
| Slot booked by another party after it was offered | explained | `test_option_taken_by_someone_else_is_explained` |
| New GPS ping puts the truck too far from a held slot | hold released at once, dock freed, driver told | `test_a_new_ping_far_away_releases_the_hold_at_once_and_tells_the_driver` |
| New ping moves the truck but it is still in time | hold kept, no message | `test_a_new_ping_that_is_still_close_keeps_the_hold` |
| Truck moves far away after the offer, before the driver picks | no hold; the system says the position changed | `test_a_ping_that_moves_the_truck_away_before_the_driver_picks_blocks_the_hold` |
| Truck moves closer | nothing is refused (GPS never makes arrival earlier) | `test_a_ping_that_moves_the_truck_closer_changes_nothing` |
| Far ping stored without the immediate re-check, then confirm | confirm refuses and releases; original stands | `test_confirming_after_a_far_ping_that_was_not_reassessed_still_refuses` |
| Any new ping | the driver is told the distance, drive time and earliest arrival | `test_every_ping_is_confirmed_to_the_driver_with_distance_and_drive_time` |
| Truck moves much closer after booking a late slot | one earlier slot is held and offered; "yes" swaps the booking | `test_a_closer_truck_is_offered_one_held_earlier_slot_and_can_switch_its_booking` |
| Several pings in a row | the same offer is not repeated | `test_the_same_offer_is_not_repeated_for_every_ping` |
| Closer, but nothing 30 minutes earlier than the current slot, or the driver's own later ETA binds | no offer | `test_no_offer_when_the_current_slot_is_already_as_early_as_it_gets` |
| Search from far away | only slots the truck can reach are offered | `test_a_search_from_far_away_does_not_offer_slots_the_truck_cannot_reach` |
| Driver with two loads, ping stored on the first, late booking on the second | the second load is still offered earlier slots, naming its order | `test_a_position_update_reaches_the_load_with_the_late_booking_even_when_it_is_not_the_first_load` |
| Dock breaks down after the offer | option blocked | `test_dock_breakdown_after_showing_blocks_the_option` |
| Slot removed from the schedule | disappears | `test_slot_removed_from_schedule_disappears` |
| Facility rules changed | flagged | `test_changed_facility_rules_are_flagged` |
| Slot lost between hold and confirm | hold released, original kept | `test_feasibility_is_rechecked_and_a_lost_slot_releases_the_hold` |
| Original already in progress | never ended | `test_an_original_already_in_progress_is_never_ended` |
| Return to the original slot after a reschedule | offered as a new option, never restored | `test_revert_to_original_is_a_fresh_option_not_an_automatic_restore` |
| Original slot taken in the meantime | reported | `test_original_slot_taken_in_the_meantime_is_reported` |
| Two critical loads and no single slot | hand-off | `test_snapshot_reality_two_critical_loads_have_no_single_slot_option` |

## F. Model failures and misbehaviour

| Situation | Expected | Test / script |
|---|---|---|
| Model claims "confirmed" with nothing committed | replaced with a safe sentence | `test_unbacked_confirmation_claims_are_replaced`; Script `show_phase8.py` part 6 |
| Model claims "held" without a live hold | replaced | `test_unbacked_hold_claims_are_replaced` |
| Describing someone else's booking | not treated as a claim | `test_describing_someone_elses_booking_is_not_a_claim_about_this_drivers` |
| Denial ("nothing is booked") | not a claim | `test_denials_are_not_mistaken_for_claims` |
| Model leaks its prompt, fully or partly | blocked | `test_prompt_canary_never_reaches_the_driver`, `test_even_a_partial_prompt_leak_is_blocked` |
| Model times out twice | safe reply plus a case | `test_model_timeouts_twice_are_handled_the_same_way` |
| Model unavailable before any commit | safe reply plus a case | `test_model_outage_before_any_commit_is_safe_and_escalated` |
| Model fails after a hold committed | still reports the committed hold | `test_model_failure_after_a_commit_still_reports_the_committed_hold` |
| Model loops forever | stopped and handed off | `test_runaway_model_is_stopped_and_handed_off` |
| More than 6 rounds / 4 tools / deadline / cost | stopped | `test_model_gets_at_most_six_rounds`, `test_at_most_four_tool_executions_even_within_one_response`, `test_request_deadline_stops_the_loop`, `test_cost_budget_stops_the_loop_before_the_next_call` |
| Rejected tool calls and the tool budget | do not consume it | `test_rejected_calls_do_not_consume_the_tool_budget` |
| Malformed tool arguments | returned to the model, not executed | `test_malformed_tool_arguments_are_returned_to_the_model_and_not_executed` |
| Provider returns invalid tool JSON | invalid call, not a crash | `test_openai_unparseable_tool_arguments_become_an_invalid_call_not_a_crash` |
| Provider rate limit or 5xx | one retry | `test_transient_error_is_retried_once` |
| Provider bad request or bad key | no retry | `test_permanent_failure_is_not_retried` |
| No model configured | same safe path | `test_unconfigured_model_takes_the_same_safe_path` |
| Model asks for a human (safety) | hand-off | `test_model_can_ask_for_a_human_for_safety` |

## G. Security and abuse

| Situation | Expected | Test / script |
|---|---|---|
| "Ignore all previous instructions..." | forged calls refused, nothing changes | `test_driver_text_cannot_widen_the_tool_allowlist`; Script `show_phase8.py` part 2 |
| Model supplies a driver id | rejected | `test_the_model_cannot_name_a_driver_or_reach_a_foreign_shipment`, `test_no_tool_schema_even_has_a_driver_id_parameter` |
| Instructions hidden in load notes | treated as data | `test_untrusted_load_notes_are_data_and_cannot_steer_the_model_into_writes` |
| Fabricated approval quote | no write | `test_a_fabricated_driver_quote_does_not_authorise_a_hold` |
| Hold and confirm in one message | refused | `test_hold_and_confirm_in_the_same_message_is_refused` |
| Two writes in one message | second refused | `test_only_one_write_per_message` |
| Old or foreign option set id | refused | `test_only_the_latest_option_set_can_be_held`, `test_foreign_option_set_id_is_refused_by_compare` |
| Another driver's thread, shipment, hold or case | refused with one error | `test_other_driver_cannot_load_or_append`, `test_only_the_owner_of_the_hold_can_confirm`, `test_driver_can_cancel_an_open_or_claimed_case_and_nobody_else_can` |
| Wrong, missing or tampered signature | 401, body unparsed | `test_bad_or_missing_signature_is_refused_before_the_body_is_parsed`, `test_tampered_body_and_missing_signature_rejected` |
| Unknown or suspended sender | refused | `test_unknown_and_suspended_senders_are_refused` |
| Stolen database copy and session tokens | useless (hash only) | `test_raw_token_is_not_stored` |
| Expired session | refused | `test_expired_session_rejected` |
| Message burst | 429 before the model | `test_burst_is_rate_limited_before_the_model_is_called` |
| Denied messages and rate budget | not charged | `test_denied_messages_do_not_consume_hourly_budget` |
| Coordinator endpoints without a token | 401 | `test_coordinator_endpoints_need_a_known_token` |
| Demo from another machine | 403 | `test_demo_routes_refuse_anyone_but_this_machine` |
| Hostile text shown on the demo page | escaped | `test_the_page_escapes_everything_it_renders` |
| Secrets and phones in logs and traces | masked | `test_redact_masks_secrets_and_phones`, `test_scrub_masks_phones_emails_secrets_and_secret_named_keys_but_keeps_dates` |
| Tracing without owner approval | off | `test_tracing_is_off_without_the_owners_approval_even_if_a_key_exists` |

## H. System failures

| Situation | Expected | Test / script |
|---|---|---|
| Failure at any write point of confirm | everything rolls back | `test_failure_at_any_write_point_rolls_everything_back`; Script `show_phase6.py` part 3 |
| Database outage before any write | nothing changes | `test_database_outage_before_any_write_changes_nothing` |
| Database dies mid-turn | nothing promised; hand-off kept in a file | `test_database_outage_mid_turn_promises_nothing_and_records_the_hand_off_elsewhere` |
| Database down at the API door | 503 inviting a retry | `test_database_outage_is_a_503_that_invites_a_retry` |
| Database fails inside the turn | safe reply | `test_database_failure_inside_the_turn_still_gets_a_safe_reply` |
| Cache (Redis) lost | rebuilt from SQL | `test_redis_loss_rebuilds_from_sql`, `test_restart_new_worker_new_cache_same_context` |
| Cache down | not ready; health still ok | `test_ready_when_everything_is_up_and_not_ready_without_the_cache` |
| Reply lost after commit, channel retries | stored reply returned | `test_response_loss_after_commit_is_recoverable_by_retry`, `test_duplicate_delivery_returns_the_stored_reply_without_another_model_call` |
| Same message id with different text | 409 | `test_same_message_id_with_different_text_is_a_conflict` |
| Worker crashed while processing | claim taken over after 60 s | `test_a_message_still_being_processed_is_reported_and_a_stale_claim_is_taken_over` |
| Unexpected exception | nothing leaks; id freed | `test_an_unexpected_error_leaks_nothing_and_frees_the_message_id` |
| Notice delivery fails | appointment untouched; retries | `test_notification_failure_never_rolls_back_the_appointment`, `test_transient_failure_retries_then_succeeds` |
| Receiver rejects for good | never retried | `test_permanent_failure_is_not_retried` (in `test_notifications.py`) |
| Delivery keeps failing (internal notices) | permanent after 3 attempts | `test_transient_failure_becomes_permanent_after_max_attempts` |
| Duplicate delivery after a crash | absorbed by the dedupe key | `test_duplicate_delivery_after_a_crash_is_absorbed_by_the_dedupe_key` |
| Cache or model state lost during expiry | hold state read from the database | `test_memory_reload_sees_live_hold_state_not_a_stale_promise` |
| Planted database corruption | detected by the invariant checks | `test_each_planted_fault_is_detected` |
| Fallback hand-off file | ingested once | `test_fallback_file_hand_offs_are_ingested_once` |

## I. Language

| Situation | Expected | Test |
|---|---|---|
| English, Hindi, Hinglish detection | correct language | `test_supported_languages_are_detected` |
| Another script | unsupported: one question, then a hand-off | `test_other_scripts_are_unsupported`, `test_unsupported_language_gets_one_choice_question_then_a_hand_off` |
| Ambiguous text | keeps the earlier language, or unknown | `test_ambiguous_input_falls_back_to_the_established_language_or_is_unknown` |
| Driver switches language mid-chat | reply follows | `test_reply_language_follows_the_driver_when_they_switch` |
| Every wording exists in all three languages | same placeholders | `test_every_template_exists_in_all_three_languages_with_the_same_placeholders` |
| Held-slot message in Hinglish and Hindi | facility named in that language | `test_hold_message_in_hindi_and_hinglish_names_the_facility`, `test_hindi_conversation_end_to_end` |
| Hold expiry notice in Hinglish | Hinglish | `test_hinglish_hold_expiry_notice_is_in_hinglish` |
| Claim detection in all languages | recognised | `test_confirmation_claims_are_recognised_in_every_language`, `test_hold_claims_are_recognised_in_every_language` |

## J. Operations (hand-off queue)

| Situation | Expected | Test |
|---|---|---|
| Hand-off creates a case with full context and no SLA | complete | `test_handoff_creates_an_open_case_with_full_context_and_no_sla` |
| Repeated hand-off | folded into the active case | `test_duplicate_handoff_folds_into_the_active_case` |
| Unknown reason code | rejected | `test_every_trigger_reason_is_accepted_and_unknown_ones_are_not` |
| Non-owner resolves | refused | `test_only_the_claiming_coordinator_resolves_and_the_driver_is_told_in_the_chat` |
| Reopen over a newer active case | refused | `test_only_a_resolved_case_can_be_reopened_and_not_over_a_newer_active_one` |
| Driver cancels their case | allowed; nobody else can | `test_driver_can_cancel_an_open_or_claimed_case_and_nobody_else_can` |
| Response time promised? | only if an SLA is approved | `test_a_response_time_is_only_promised_when_an_sla_is_approved` |
| Case history edited or deleted | refused by triggers | `test_case_events_cannot_be_changed_or_deleted` |
| Inconsistent case rows | refused by the database | `test_the_database_refuses_inconsistent_case_rows` |
| Coordinator CLI | list, show, claim, resolve | `test_cli_lists_shows_claims_and_resolves` |

## J2. Partner warehouse by email

| Situation | Result | Test |
|---|---|---|
| Partner replies APPROVE from a known contact | slot booked as `WAREHOUSE`, original ended in the same transaction | `test_approve_books_with_warehouse_source_and_ends_original_atomically` |
| Crash part-way through booking | everything rolls back; a retry still works | `test_fault_after_original_ended_rolls_back_everything` |
| Reply is "not approved" or quotes "APPROVE or DECLINE" | ambiguous: no booking, case opened | `test_parse_decision`, `test_ambiguous_reply_escalates_and_request_stays_open` |
| Reply from someone who is not a contact | no booking, case opened | `test_wrong_sender_escalates_and_does_not_book` |
| Reply for another request's reference | no booking | `test_valid_sender_but_other_requests_ref_does_not_book_this_request` |
| Same email read twice | booked once | `test_same_message_twice_books_once`, `test_poll_twice_same_mail_books_once` |
| Approval after expiry | no booking, case opened | `test_approval_after_expiry_does_not_book` |
| Truck already unloading at the original | no booking, case opened | `test_original_in_progress_escalates_without_booking` |
| Partner slot taken meanwhile | request fails, original kept, case opened | `test_slot_already_taken_ends_request_failed_and_escalates` |
| SPF/DKIM/DMARC failure | no booking, case opened | `test_failed_authentication_escalates` |
| Driver chat asks for a partner slot | request recorded, reply worded from stored state, model claim blocked | `test_answer_is_rendered_from_stored_state_not_the_models_claim` |

## J3. Change of facility

| Situation | Expected | Test |
|---|---|---|
| Approved | a slot is held there for 15 minutes and the driver is told | `test_approval_holds_at_the_new_facility_long_enough_to_be_seen_and_tells_the_driver` |
| Nothing can be offered there | the load goes back and a case is opened | `test_no_slot_there_sends_the_load_back_and_opens_a_case` |
| Offer lapses, or every slot declined | the load goes back | `test_a_lapsed_offer_sends_the_load_back_at_the_next_sweep`, `test_declining_every_slot_at_the_new_site_sends_the_load_back` |
| A hold for the old site after the destination changed | cannot be confirmed | `test_a_hold_for_the_old_site_cannot_be_confirmed_once_the_destination_changed` |
| Follow-up crashes after approval | load put back, case opened | `test_a_follow_up_that_crashes_puts_the_load_back_and_opens_a_case` |
| Rejected | the driver is told | `test_a_rejection_is_queued_for_the_drivers_phone` |

## J4. Telegram delivery and the signed channel

| Situation | Expected | Test |
|---|---|---|
| A queued notice for a linked driver | sent to their chat | `test_driver_notice_goes_to_bound_chat` |
| Driver links late | no backlog of old messages | `test_a_late_binding_driver_gets_no_old_backlog` |
| Notice older than 30 minutes | marked, not sent | `test_a_notice_older_than_thirty_minutes_is_marked_and_not_sent` |
| Telegram unreachable for five minutes | message still arrives | `test_a_message_gets_through_after_a_five_minute_outage` |
| Earlier message waiting to be retried | later ones wait, order kept | `test_later_messages_for_the_driver_wait_their_turn` |
| Database busy just after sending | not sent twice | `test_a_locked_database_after_sending_does_not_send_twice` |
| Unreadable row in the queue | does not block the others | `test_a_notice_with_a_bad_timestamp_does_not_block_the_ones_behind_it` |
| Send error | logged without the bot token | `test_a_failing_pass_is_logged_without_secrets_and_the_next_one_sends` |
| No webhook secret configured | forged signed requests refused | `test_a_forged_driver_request_is_refused_when_no_secret_is_set` |
| Server started on the sample database | refuses to start | `test_the_server_does_not_start_on_the_sample_database` |

## J5. Warehouse actions

| Situation | Expected | Test |
|---|---|---|
| Case resolved with a slot | the booking moves | `test_resolving_with_a_slot_moves_the_booking` |
| That slot is already taken | nothing changes | `test_a_taken_slot_changes_nothing` |
| Truck cannot reach the slot, or is already on site | refused | `test_a_slot_before_the_truck_can_arrive_is_refused`, `test_a_truck_recorded_on_site_keeps_its_booking` |
| Operations writes to a driver; a failed message | sent; can be sent again | `test_operations_can_write_after_the_case_is_closed`, `test_a_failed_message_can_be_sent_again` |
| No crew number entered | no slots are created | `test_no_slots_appear_until_a_warehouse_has_a_crew_number` |
| Crew number saved | 14 days of slots | `test_saving_the_crew_number_creates_fourteen_days_of_slots` |
| Dock blocked with a booking on it | moved to the proposed slot, or freed | `test_block_moves_the_booking`, `test_nothing_fits_frees_and_keeps_the_shipment` |
| Things changed after the warning | nothing saved | `test_a_changed_plan_saves_nothing` |
| Slot freed | booking cancelled, shipment kept | `test_free_slot_cancels_the_booking_and_keeps_the_shipment` |
| Driver deleted, then restored | shipments cancelled; they do not return | `test_delete_cancels_shipments_frees_slots_and_hides_the_driver`, `test_restore_brings_the_driver_back_but_not_the_shipments` |

## J6. Passes, gate and password sign-in

| Situation | Expected | Test |
|---|---|---|
| Right pass sent in chat | shipment confirmed without the model | `test_the_right_pass_confirms_without_calling_the_model` |
| Order number alone | does nothing | `test_an_order_number_alone_does_nothing` |
| Fifth wrong pass | shipment locked, one case | `test_fifth_wrong_pass_locks_and_opens_one_case` |
| Two guards check in at once | one check-in | `test_two_guards_at_once_make_one_check_in` |
| Truck stays past the window | one case, nothing released | `test_an_overstay_opens_one_case_and_releases_nothing` |
| Password link used twice | works once | `test_link_sets_password_once` |
| Five wrong passwords | locked 15 minutes | `test_five_failures_lock_15_minutes` |

## K. Gaps worth knowing

* The rate limit's increment and rollback are not atomic across processes (documented in `rate_limit.py`).
* The demo console has no rule-based assistant: chat needs a configured model (the stand-in is used by tests, load runs and the Telegram trial).
* The held-slot message is system-worded, so the model cannot add an explanation to it.
* Hindi (Devanagari) has one recorded live-model run (2026-11-26); Hindi and Hinglish wording needs native-speaker review.
* Operational thresholds that depend on a real model, real traffic or a real labour feed are not demonstrated by scripted runs.
