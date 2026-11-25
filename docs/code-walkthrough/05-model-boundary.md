# 5. The model boundary

Files: `model_gateway.py`, `tools.py`, `prompts.py`, `language.py`, `conversation.py`, `stub_model.py`.

## The idea

The model interprets and drafts. **Code decides what is allowed and what is true.** Four mechanisms enforce that.

1. The model sees only the tools the current state allows, and every tool call passes checkpoints.
2. Anything stating a booking fact is worded by the system from committed rows.
3. Model text that claims something the database does not back is replaced.
4. Hard caps on rounds, tool runs, time and cost stop a runaway model.

## The loop (`model_gateway.py`)

* `ModelGateway` is a one-method protocol. Implementations: `ScriptedGateway` (tests, replay), `AnthropicAdapter`,
  `OpenAIAdapter` (both over `httpx`, tested with mock transports), and `stub_model.StubGateway` (a rule-based stand-in for
  demos and load tests; it says nothing about model quality).
* `complete_with_retry`: one retry on a transient error (rate limit, 5xx, network, timeout). A permanent error (bad request,
  bad key) is not retried. The final failure is `ModelUnavailable`, and callers then promise nothing.
* `run_bounded_loop` is a **LangGraph** state graph with two nodes, `model` and `tools`. The caps are enforced in the node
  code, not by graph settings: 6 rounds, 4 tool executions (rejected calls do not count), 30 seconds for the whole message,
  and a cost budget. The graph's recursion limit is only a backstop.
* LangSmith tracing is forced off unless explicitly approved and configured (`langsmith_trace.py`). See layer 7.
* Model calls never happen inside a database transaction.

## Tools (`tools.py`)

| Kind | Tools |
|---|---|
| Read | `list_my_shipments`, `get_shipment_status`, `compute_eta`, `search_slot_options`, `compare_option_set`, `offer_original_slot` |
| Write | `request_hold`, `confirm_reschedule`, `release_pending_change`, `cancel_appointment` |
| Escalate | `escalate_to_operations` |

`ToolRegistry.execute` runs every call through these checkpoints, in order:

1. **Allowed?** A tool not offered in this state is `TOOL_NOT_ALLOWED`. Availability rules: hold and compare need an existing menu;
   confirm needs a hold from an *earlier* message; release needs a hold.
2. **Valid arguments?** Pydantic models with `extra="forbid"`: an invented `driver_id` field is `INVALID_ARGUMENTS`.
   **No tool schema has an identity parameter at all.**
3. **For write tools: is the `driver_quote` inside the driver's *current* message?** Otherwise `APPROVAL_NOT_PRESENT`. The
   model cannot invent permission.
4. **At most one committed write per message** (`ONE_WRITE_PER_MESSAGE`).
5. The handler runs. Authorisation errors return a code; value errors return `INVALID_ARGUMENTS`.

Handlers always check ownership with the trusted driver context. A hand-off can be raised by code (no slot, contradictory
data, approval needed, cancellation rules) or requested by the model for safety, regulated loads or uncertainty.

## The prompt (`prompts.py`)

The system prompt is written as boundaries. It contains a random **canary** string per process. If the canary, or any
substantial line of the prompt, appears in a reply, `leaks_prompt` is true and the reply is blocked. The prompt tells the
model that text in `untrusted_text` fields (load notes, remarks) is data, never instructions.

## Wording (`language.py`)

* Languages: English, Hindi (Devanagari) and Hinglish (Hindi in Latin letters). `detect_language` counts script letters
  (Devanagari 30% or more is Hindi; 30% other scripts is unsupported) and common Hindi/English marker words.
* `T` is the wording table for every system-stated fact, with all three languages: hold placed, hold expired, hold released,
  confirmed, cancelled, the **held-slot offer**, hand-off, case updates, outage text, and the two guard replacements.
* `claims_confirmed` / `claims_held` detect model text that claims a booking. Denials ("nothing has been booked") and talk
  about someone else's booking are not counted as claims.

## The workflow (`conversation.py`)

`handle_message` runs one turn:

1. Save the driver's original text (never altered).
2. Language: read an answer to "English or Hindi?"; otherwise detect. Unsupported, or unclear twice: hand-off.
3. Build the prompt with **system facts** (live hold, active shipments, latest option set) and run the bounded loop.
4. `_finalize` builds the reply:
   * confirmed / cancelled / hold placed / released: **system wording from the committed row**;
   * a hand-off raised: record the case, say so with the reference;
   * a search that found slots: the system holds the first and words the offer itself (D93);
   * otherwise the model's text, **after the output guard** (`_guard`): prompt leaks and unbacked hold/confirmation claims are
     replaced with a safe sentence;
   * model unavailable or a loop cap hit: a hand-off or an "I cannot check right now" reply.
5. A hold that lapsed while the driver was away is announced first, once.
6. Database or service failures anywhere in the turn become a safe reply and a hand-off recorded in the fallback file.

## Edge cases and failure scenarios

| Situation | Result | Test |
|---|---|---|
| Driver text tries to widen the tool list | cannot | `test_driver_text_cannot_widen_the_tool_allowlist` |
| Model invents a `driver_id` | rejected | `test_the_model_cannot_name_a_driver_or_reach_a_foreign_shipment`, `test_no_tool_schema_even_has_a_driver_id_parameter` |
| Instructions hidden in load notes | treated as data | `test_untrusted_load_notes_are_data_and_cannot_steer_the_model_into_writes` |
| Fabricated approval quote | no write | `test_a_fabricated_driver_quote_does_not_authorise_a_hold` |
| Odd spacing or case in the real quote | still authorises | `test_the_real_quote_does_authorise_it_even_with_odd_spacing_and_case` |
| Hold and confirm in one message | refused | `test_hold_and_confirm_in_the_same_message_is_refused` |
| Two writes in one message | second refused | `test_only_one_write_per_message` |
| Hold an old menu | refused | `test_only_the_latest_option_set_can_be_held` |
| Model claims "confirmed" with nothing committed | replaced | `test_unbacked_confirmation_claims_are_replaced` |
| Model claims "held" with no live hold | replaced | `test_unbacked_hold_claims_are_replaced` |
| Model leaks the prompt (even partly) | blocked | `test_prompt_canary_never_reaches_the_driver`, `test_even_a_partial_prompt_leak_is_blocked` |
| Model times out twice | safe reply + case | `test_model_timeouts_twice_are_handled_the_same_way` |
| Model fails after a commit | still reports the committed hold | `test_model_failure_after_a_commit_still_reports_the_committed_hold` |
| Model loops forever | stopped and handed off | `test_runaway_model_is_stopped_and_handed_off` |
| Malformed tool arguments | returned to the model, not executed | `test_malformed_tool_arguments_are_returned_to_the_model_and_not_executed` |
| Database dies mid-turn | nothing promised, hand-off kept in a file | `test_database_outage_mid_turn_promises_nothing_and_records_the_hand_off_elsewhere` |
| Round / tool / deadline / cost caps | enforced | `test_model_gets_at_most_six_rounds`, `test_at_most_four_tool_executions_even_within_one_response`, `test_request_deadline_stops_the_loop`, `test_cost_budget_stops_the_loop_before_the_next_call` |
| Provider returns bad tool JSON | invalid call, not a crash | `test_openai_unparseable_tool_arguments_become_an_invalid_call_not_a_crash` |
| Unsupported language | one question, then a hand-off | `test_unsupported_language_gets_one_choice_question_then_a_hand_off` |
| Driver switches language | reply follows | `test_reply_language_follows_the_driver_when_they_switch` |
| Offer wording | system-worded from the held row | `test_delay_report_holds_the_first_option` |
