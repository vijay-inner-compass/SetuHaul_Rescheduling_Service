# 7. Evaluation and tracing

Files: `evaluation.py`, `langsmith_trace.py`, `proof.py`, `scale_fixture.py`, `scripts/replay_traces.py`,
`scripts/run_live_model_scenario.py`, `scripts/run_integrity_proof.py`, `scripts/run_scale_profile.py`.

## Why this layer exists

Tests prove the code. **Traces prove behaviour**: what the model asked for, what the deterministic layers answered, and what
the driver was told. A trace can be replayed offline, so a change to the rules shows up as a difference, without calling a model.

## Traces (`evaluation.py`)

* `Trace` > `TurnTrace` > `RoundTrace` > `ToolEvent`. A trace is **live** (a real model; evidence about that model) or **scripted**
  (a regression baseline; says nothing about model quality).
* `RecordingGateway` wraps any gateway and keeps every call and provider error.
* `sanitize_trace` replaces run-specific ids with stable placeholders (`$OPT1`, `$HOLD1`, `$CASE1`) and masks phone numbers,
  emails and secrets, while keeping dates.
* `classify_failure` labels a turn: `INPUT`, `TRAJECTORY`, `DECISION`, `DEPENDENCY`, `TECHNICAL_OUTCOME`,
  `INSUFFICIENT_EVIDENCE`.

## Evaluators

Plain functions over a trace (they run in CI with no model):

| Evaluator | Checks |
|---|---|
| `identity` | no executed tool call carried a driver or thread id |
| `tool_order` | at most one committed write per message; hold only after options; confirmation only after an earlier hold |
| `explicit_approval` | every committed write is backed by words in the driver's own message |
| `option_set_ownership` | holds and comparisons used a menu this conversation was shown |
| `state_backed_wording` | any reply claiming a hold or confirmation is backed by committed state |
| `round_caps` | within the round and tool limits |
| `handoff_complete` | hand-offs carry language, tools, recent turns and shipment |

## Replay

`replay_trace` feeds the **recorded model outputs** back through the real deterministic layers in a fresh database and compares
reply text, status, tool results and verified flags. A mismatch means a rule changed. It also re-runs the evaluators and the
database invariants. It never calls a live model. `python scripts/replay_traces.py` replays every file in `tests/traces/`.

## LangSmith (`langsmith_trace.py`)

Off by default. It switches on only when **both** `SETUHAUL_LANGSMITH=approved` and `LANGSMITH_API_KEY` are set. Global LangChain
tracing is forced off on every run; when enabled, an explicit tracer is attached whose client **scrubs inputs, outputs and
metadata** before anything leaves the machine. `SETUHAUL_LANGSMITH_HIDE_TEXT=1` replaces all message text with a length marker.
A tracing failure never breaks a turn.

## Concurrency proof and scale (`proof.py`, `scale_fixture.py`)

* `run_competing_requests` runs operations at the same instant in threads or separate processes, each on its own WAL connection,
  and classifies each result. `scripts/run_integrity_proof.py` repeats this, adds a Hypothesis state machine and writes the
  report.
* `make_scale_fixture` generates about 360 labelled loads (`GEN-` ids, source `GENERATED_SCALE_FIXTURE`). It is not SetuHaul data.
  `scripts/run_scale_profile.py` drives it with a spike, a ten-driver burst for one slot, Locust and write saturation.

## Edge cases and failure scenarios

| Situation | Result | Test |
|---|---|---|
| A deterministic rule changes | replay shows a mismatch | `test_replay_detects_a_changed_tool_result`, `test_replay_detects_a_change_in_the_ranking_rule`, `test_replay_detects_a_change_in_the_checkpoint_rules` |
| Replay with no network | no HTTP attempted | `test_replay_never_touches_the_network` |
| Trace contains ids or phone numbers | sanitiser removes them | `test_committed_traces_hold_no_run_specific_ids_or_personal_data` |
| Model passes an identity field | flagged unless rejected | `test_identity_attempt_must_be_rejected` |
| Hold without options / confirm in the same message | flagged | `test_hold_without_options_and_confirm_in_same_message_are_flagged` |
| Reply claims an unbacked booking | flagged | `test_unbacked_booking_claims_are_flagged` |
| Tracing without owner approval | off, even with a key | `test_tracing_is_off_without_the_owners_approval_even_if_a_key_exists` |
| Tracing client cannot be built | the turn still works | `test_a_client_that_cannot_be_built_never_breaks_a_turn` |
| Secrets or phones in traced text | masked | `test_scrub_masks_phones_emails_secrets_and_secret_named_keys_but_keeps_dates` |
| Live scenario run without a configured model | refuses, calls nothing | `test_live_scenario_refuses_to_run_without_an_approved_model` |
| Lock held by another connection | `LOCK_TIMEOUT`, never a false success | `test_lock_timeouts_are_never_reported_as_conflicts_or_success` |
| Planted database faults | each is detected | `test_each_planted_fault_is_detected` |
