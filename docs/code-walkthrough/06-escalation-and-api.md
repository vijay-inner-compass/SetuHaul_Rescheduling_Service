# 6. Escalation and the HTTP API

Files: `escalation.py`, `coordinator.py`, `api.py`.

## Escalation (`escalation.py`)

A hand-off is a **case in a queue owned by coordinators**.

* Lifecycle: `OPEN` → `CLAIMED` → `RESOLVED`, or `CANCELLED` by the driver. A resolved case can be reopened within 24 hours; after
  that a new hand-off opens a new case. Every transition is an immutable event.
* `HandoffRequest` carries a reason code from a fixed list, the driver, thread and shipment, a summary and a context
  (language, recent messages, tools used, ETA, what was missing).
* `create_handoff` computes a **dedupe key** (driver, thread, shipment, reason). If an active case has that key the request is
  folded into it as a `DUPLICATE_REQUEST` event instead of creating a second case. The database also enforces one active
  case per key with a partial unique index.
* `claim_case`: `OPEN` only; two coordinators racing produce one owner; the second is told who holds it. `resolve_case`:
  only the claiming coordinator, with a known resolution code. From the web, `ASSIGNED_SLOT` must name a slot, which `assign.py`
  books in the same transaction; any other outcome may not name one. `cancel_case_by_driver` and `reopen_case` follow the rules
  above.
* The driver is told in the chat, in their language, when a case is claimed, resolved or cancelled; claim and resolve are also
  queued for their phone when the case names a load. **No response time is
  promised** unless operations has approved an SLA (`escalation_sla_minutes`).
* **Fallback:** if the database is down, `JsonlEscalationSink` appends the hand-off to a file and flushes it to disk.
  `ingest_fallback_file` loads it back later, once, and renames it.

`coordinator.py` is a small CLI over the queue: `list`, `show`, `claim`, `resolve`, `reopen`.

## HTTP API (`api.py`)

Order of work on a driver message, **cheapest and safest first, always before the model**:

1. Verify the HMAC signature over the raw bytes. Bad or missing: 401. Nothing has been parsed.
2. Validate the body (`from`, `message_id`, `text` up to 2000 characters; unknown fields rejected): 422.
3. Look up the sender. Unknown or suspended: 403.
4. **Duplicate check** on (driver, message id): the same message with the same text returns the stored reply with
   `duplicate: true`; the same id with different text is 409; a message still processing is 409 `IN_PROGRESS`, unless the claim is
   older than 60 seconds, in which case it is taken over.
5. **Rate limit** (after the duplicate check, so a replay does not use budget): 429 with `Retry-After`.
6. Claim the message id, run the conversation, store the reply under the id, return it.
7. Any failure frees the id so the channel's retry can succeed.

Other routes:

| Route | Purpose |
|---|---|
| `GET /health` | the process is up (checks nothing else) |
| `GET /ready` | database and cache both usable (503 with a reason otherwise) |
| `GET /v1/driver/status` | signed, timestamp must be within 5 minutes, read-only |
| `GET/POST /v1/coordinator/cases...` | list, show, claim, resolve, reopen; bearer token compared in constant time |

Errors always carry `{status, reason_code, request_id}`, and may add a `message` and `problems` saying what to fix
([refusal messages](../subsystems/refusal-messages.md)). The web, admin, warehouse, gate and Telegram routes are registered from `auth/`. No stack trace, prompt, SQL or file path leaves the process. Docs
and schema endpoints are switched off. `create_app_from_env` builds the app from environment variables only.

## Edge cases and failure scenarios

| Situation | Result | Test |
|---|---|---|
| Health when the database is gone | still "ok" | `test_health_is_alive_even_when_the_database_is_gone` |
| Ready without the cache | 503 | `test_ready_when_everything_is_up_and_not_ready_without_the_cache` |
| Duplicate delivery | stored reply, no second model call | `test_duplicate_delivery_returns_the_stored_reply_without_another_model_call` |
| Duplicates and the rate limit | not charged | `test_duplicates_do_not_use_up_the_rate_limit` |
| Same id, different text | 409 | `test_same_message_id_with_different_text_is_a_conflict` |
| Stuck message | reported, then taken over after 60 s | `test_a_message_still_being_processed_is_reported_and_a_stale_claim_is_taken_over` |
| Two simultaneous duplicates | model runs once | `test_simultaneous_duplicates_run_the_model_once` |
| Burst of messages | 429 before the model is called | `test_burst_is_rate_limited_before_the_model_is_called` |
| Database down at the door | 503 inviting a retry | `test_database_outage_is_a_503_that_invites_a_retry` |
| Database fails inside the turn | safe reply | `test_database_failure_inside_the_turn_still_gets_a_safe_reply` |
| No model configured | same safe path | `test_unconfigured_model_takes_the_same_safe_path` |
| Unexpected exception | nothing leaks, id freed | `test_an_unexpected_error_leaks_nothing_and_frees_the_message_id` |
| Status request too old or unsigned | refused | `test_driver_status_is_signed_fresh_and_limited_to_the_drivers_own_data` |
| Coordinator without a token | 401 | `test_coordinator_endpoints_need_a_known_token` |
| Two coordinators claim at once | one winner, 409 names the owner | `test_racing_claims_over_http_have_one_winner`, `test_coordinator_claim_conflict_is_a_409_naming_the_owner` |
| Duplicate hand-off | folded into the active case | `test_duplicate_handoff_folds_into_the_active_case` |
| Non-owner resolves | refused | `test_only_the_claiming_coordinator_resolves_and_the_driver_is_told_in_the_chat` |
| Reopen after the window or over a newer case | refused | `test_only_a_resolved_case_can_be_reopened_and_not_over_a_newer_active_one` |
| Database down during a hand-off | recorded in the file, ingested once | `test_fallback_file_hand_offs_are_ingested_once` |
| Inconsistent case rows | refused by the database | `test_the_database_refuses_inconsistent_case_rows` |
