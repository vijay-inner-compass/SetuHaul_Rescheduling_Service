---
title: Guardrails
type: reference
created: 2026-11-28
updated: 2026-11-28
sources:
  - code: src/setuhaul/api.py
  - code: src/setuhaul/identity.py
  - code: src/setuhaul/rate_limit.py
  - code: src/setuhaul/auth/telegram_routes.py
  - code: src/setuhaul/dock_pass.py
  - code: src/setuhaul/pass_check.py
  - code: src/setuhaul/prompts.py
  - code: src/setuhaul/tools.py
  - code: src/setuhaul/model_gateway.py
  - code: src/setuhaul/language.py
  - code: src/setuhaul/conversation.py
  - code: src/setuhaul/escalation.py
  - code: src/setuhaul/memory.py
  - code: src/setuhaul/integrity.py
  - code: src/setuhaul/observability.py
  - code: src/setuhaul/langsmith_trace.py
  - code: migrations/002_appointment_holds.sql
  - code: migrations/003_idempotency_events_outbox.sql
  - code: migrations/004_escalation_queue.sql
  - code: migrations/011_auth_roles.sql
  - code: migrations/004_escalation_queue.sql
  - code: migrations/011_auth_roles.sql
verified_at: 8a5487e
---

# Guardrails

Every control between a driver's message and a change to a booking, in the order a message meets them. The rule behind them:
the model interprets and drafts; code and the database decide what is allowed and what is true. Checks on these controls
are in [[evals]]. The narrative version is [model-contract.md](../model-contract.md).

## 1. Before the model

| Guardrail | What it does | Where | Test |
|---|---|---|---|
| Channel signature | HMAC-SHA256 over the raw body, checked before parsing; with no secret set, everything is refused | `src/setuhaul/identity.py:72` | `test_bad_or_missing_signature_is_refused_before_the_body_is_parsed`, `test_a_signature_made_with_an_empty_key_is_refused` |
| Telegram secret | the secret header is checked before the body is read; an unbound sender never reaches the engine | `src/setuhaul/auth/telegram_routes.py:151` | `test_missing_or_wrong_secret_header_is_401`, `test_unbound_sender_gets_share_contact_prompt_and_engine_not_called` |
| Identity | the driver comes from the verified channel, never from the message or the model; unknown and suspended senders are refused | `src/setuhaul/identity.py` | `test_unknown_and_suspended_senders_are_refused`, `test_client_supplied_session_id_is_never_authorization` |
| Input shape | unknown fields refused; text 1 to 2,000 characters | `src/setuhaul/api.py:287` | `test_validly_signed_garbage_is_a_422_and_client_supplied_identity_is_refused` |
| Duplicates | a repeated message id returns the stored reply without a model call | `src/setuhaul/api.py:527` | `test_duplicate_delivery_returns_the_stored_reply_without_another_model_call`, `test_simultaneous_duplicates_run_the_model_once` |
| Rate limit | 20 a minute and 100 an hour per driver and channel; refused messages use no budget | `src/setuhaul/rate_limit.py:89` | `test_burst_is_rate_limited_before_the_model_is_called` |
| Pass codes | checked by the app, then blanked before the message is saved, put in a case or sent to a model; the fifth wrong code locks confirmation and opens a case | `src/setuhaul/dock_pass.py:79`, `src/setuhaul/pass_check.py:19` | `test_the_code_is_never_kept`, `test_fifth_wrong_pass_locks_and_opens_one_case` |
| Language | an unsupported or unreadable message gets one language question, then a person, with no model call | `src/setuhaul/conversation.py:302` | `test_unsupported_language_gets_one_choice_question_then_a_hand_off` |

## 2. What the model is told and given

| Guardrail | What it does | Where | Test |
|---|---|---|---|
| Boundary prompt | may and must-never rules; `untrusted_text` is data; safety, regulated loads and doubt go to a person | `src/setuhaul/prompts.py:23` | `test_system_prompt_tells_the_model_notes_are_data` |
| State-based tool list | only tools valid now are offered: hold and compare need shown options, confirm and decline need a hold from an earlier message and are withheld while a cancel question waits | `src/setuhaul/tools.py:904` | `test_driver_text_cannot_widen_the_tool_allowlist` |
| No identity parameter | no tool schema accepts a driver id; handlers check ownership | `src/setuhaul/tools.py:918` | `test_no_tool_schema_even_has_a_driver_id_parameter`, `test_the_model_cannot_name_a_driver_or_reach_a_foreign_shipment` |
| Untrusted notes | load notes and others' remarks are returned under `untrusted_text` | `src/setuhaul/tools.py:469` | `test_untrusted_load_notes_are_data_and_cannot_steer_the_model_into_writes` |

## 3. On every tool call

Checked in this order by `ToolRegistry.execute` (`src/setuhaul/tools.py:1024`).

| Check | Refusal | Test |
|---|---|---|
| Tool offered in this state | `TOOL_NOT_ALLOWED` | `test_driver_text_cannot_widen_the_tool_allowlist` |
| Arguments valid, no extra fields | `INVALID_ARGUMENTS` | `test_malformed_tool_arguments_are_returned_to_the_model_and_not_executed` |
| Write: `driver_quote` is in the current message | `APPROVAL_NOT_PRESENT` | `test_a_fabricated_driver_quote_does_not_authorise_a_hold` |
| Write: at most one committed change per message | `ONE_WRITE_PER_MESSAGE` | `test_only_one_write_per_message` |
| Hold: only the latest option set | `NOT_LATEST_OPTION_SET` | `test_only_the_latest_option_set_can_be_held` |
| Decline: the message does not also agree | `AMBIGUOUS`, nothing changes | `test_an_agreeing_message_can_never_decline`, `test_a_negated_agreement_word_is_not_agreement` |
| Confirm: a clear yes, with no refusal, "wait", "but" or question (`src/setuhaul/tools.py:699`) | `NEEDS_YES`, nothing is confirmed | `test_a_confirm_call_on_a_message_that_does_not_agree_books_nothing`, `test_anything_else_does_not_confirm` |
| Cancel: two messages; the system asks, a clear yes in the next message within 10 minutes cancels (`src/setuhaul/tools.py:820`) | `NEEDS_YES` or `NOT_AGREED`, nothing is cancelled | `test_the_first_cancel_message_only_asks`, `test_a_no_in_the_next_message_cancels_nothing`, `test_the_question_lapses_with_time` |

> ⚠️ The agreement, confirm and cancel checks (`src/setuhaul/tools.py:650`) are word patterns in English, Hinglish and Hindi. A missed negation
> once booked a refused slot ("nahi chalega"). Add a case to `tests/test_negated_agreement.py` or `tests/test_write_intent.py` with any change to the words.

> ⚠️ The quote check proves the words are in the message, not that they mean what the model took them to mean.

## 4. Limits on a model turn

Defaults of `ModelPolicy` (`src/setuhaul/model_gateway.py:106`), provisional until approved.

| Limit | Value | Test |
|---|---|---|
| Model rounds per message | 6 | `test_model_gets_at_most_six_rounds` |
| Tool runs per message (refused calls do not count) | 4 | `test_at_most_four_tool_executions_even_within_one_response` |
| One provider call | 8 s, one retry on a transient error, none on a permanent one | `test_transient_error_is_retried_once`, `test_permanent_failure_is_not_retried` |
| Whole message | 30 s | `test_request_deadline_stops_the_loop` |
| Output | 800 tokens, temperature 0 | none cited |
| Cost per message | USD 0.05 | `test_cost_budget_stops_the_loop_before_the_next_call` |

> ⚠️ The cost stop does nothing until `SETUHAUL_MODEL_PRICE_IN_PER_MTOK` and `..._OUT_PER_MTOK` are set: unpriced calls
> are estimated at zero.

> ⚠️ The 30-second deadline is checked before each model call, so a call already running can end after it.

## 5. On the reply

| Guardrail | What it does | Where | Test |
|---|---|---|---|
| System wording | a hold, confirmation, cancellation, release, change request or hand-off, and the cancel question or "not confirmed" line, are worded by the system; the model's text is dropped | `src/setuhaul/conversation.py:387` | `test_hold_wording_comes_from_the_committed_hold_not_the_model` |
| Prompt leak | the per-process canary, or the first 40 characters of a long prompt line, blocks the reply | `src/setuhaul/prompts.py:80` | `test_even_a_partial_prompt_leak_is_blocked` |
| Unbacked confirmation | a "confirmed" claim with nothing committed is replaced; after a status read it may name only the times read | `src/setuhaul/conversation.py:278` | `test_unbacked_confirmation_claims_are_replaced`, `test_a_status_read_does_not_back_a_claim_about_a_different_slot` |
| Unbacked hold | a "held" claim with no live hold is replaced | `src/setuhaul/conversation.py:278` | `test_unbacked_hold_claims_are_replaced` |
| Lapsed hold | announced first, once | `src/setuhaul/conversation.py:387` | `test_hold_that_lapsed_is_reported_instead_of_replaying_the_promise` |

> ⚠️ Claims are detected by regular expressions (`src/setuhaul/language.py`, `claims_confirmed`, `claims_held`). A
> paraphrase can pass. A denial ("not booked") is discounted only within its own clause. Other free model text, such as an invented time or dock with no claim word, is not checked.

> ⚠️ `leaks_prompt` compares against the lines of `SYSTEM_PROMPT`. Reword the prompt and re-run `tests/test_injection.py`.

## 6. On the booking itself

These hold whatever the model does.

| Guardrail | What it does | Where | Test |
|---|---|---|---|
| One holder per slot, one current booking and one hold per shipment | unique indexes in the database | `migrations/002_appointment_holds.sql:51` | `test_unique_index_is_the_backstop_when_the_read_was_stale`, `test_ten_synchronized_threads_one_slot_exactly_one_winner` |
| Stale offers | an expired or changed option is not held | `src/setuhaul/tools.py:599` | `test_expired_option_set_is_refused`, `test_option_that_changed_since_it_was_shown_is_not_held` |
| Two-step booking | hold and confirm cannot happen in one message | `src/setuhaul/tools.py:904` | `test_hold_and_confirm_in_the_same_message_is_refused` |
| Append-only history | appointment, case and audit events cannot be updated or deleted | `migrations/003_idempotency_events_outbox.sql:36`, `migrations/004_escalation_queue.sql:64`, `migrations/011_auth_roles.sql:104` | none cited |
| Invariant checker | 12 queries that return rows breaking a booking or case rule | `src/setuhaul/integrity.py:32` | `test_each_planted_fault_is_detected` |

## 7. When something fails, and when a person takes over

| Situation | Result | Test |
|---|---|---|
| Model unavailable before a commit | safe reply, nothing booked or held, case opened | `test_model_outage_before_any_commit_is_safe_and_escalated` |
| Model fails after a commit | the committed state is still reported | `test_model_failure_after_a_commit_still_reports_the_committed_hold` |
| The model step crashes, or the provider's answer is unreadable | treated as model unavailable | `test_a_crash_in_the_model_step_still_answers_and_hands_off`, `test_an_unreadable_provider_answer_is_a_model_error` |
| A loop limit is hit | hand-off, `MODEL_LOOP_LIMIT` | `test_runaway_model_is_stopped_and_handed_off` |
| Database fails mid-turn | nothing promised; hand-off written to a file | `test_database_outage_mid_turn_promises_nothing_and_records_the_hand_off_elsewhere` |
| Hand-offs written to that file | the maintenance sweep loads them; unloadable lines are set aside | `test_the_sweep_loads_fallback_handoffs_each_time_there_are_some`, `test_a_broken_line_is_set_aside_and_the_rest_are_loaded` |
| Redis unreachable | the driver is still served from the database | `test_a_driver_message_is_still_handled_with_redis_down` |

Hand-off reason codes are the closed set `REASON_CODES` (`src/setuhaul/escalation.py:68`); an unknown code is rejected.

> ⚠️ `SAFETY` and `REGULATED_LOAD` depend on the model recognising the situation and calling the escalate tool. No rule
> in code detects them.

> ⚠️ The rate limit fails open: while Redis is unreachable, messages are not counted (`src/setuhaul/rate_limit.py:89`).
> The model cost stop is then the only spend control, and it needs configured prices.

## 8. Data leaving the service

| Guardrail | What it does | Where | Test |
|---|---|---|---|
| Log redaction | secret-named keys and phone-like numbers masked | `src/setuhaul/observability.py:130` | `test_redact_masks_secrets_and_phones` |
| Provider refusal log | keys hidden in the logged reason | `src/setuhaul/model_gateway.py:476` | `test_the_providers_reason_is_logged_without_keys` |
| External tracing | forced off unless switched on, approved and keyed | `src/setuhaul/model_gateway.py:551` | `test_tracing_is_off_without_the_owners_approval_even_if_a_key_exists` |
| Tracing scrubber | phones, emails and secrets masked; message text hidden with `SETUHAUL_LANGSMITH_HIDE_TEXT=1` | `src/setuhaul/langsmith_trace.py:118` | `test_scrub_masks_phones_emails_secrets_and_secret_named_keys_but_keeps_dates`, `test_hide_text_replaces_message_text_but_keeps_structure` |

> ⚠️ Apart from pass codes, nothing is redacted before the model: driver text and load notes are sent as written.

## Not covered

* No content-moderation or toxicity filter, and no second model that reviews replies.
* Native speakers have not reviewed the Hindi or Hinglish wording or word patterns.
* The rate limit is approximate across processes (`src/setuhaul/rate_limit.py`).
* Web sign-in and staff roles are a separate area: see [[sign-in]].
