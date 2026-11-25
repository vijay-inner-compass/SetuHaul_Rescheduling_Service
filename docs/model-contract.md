# Model Contract (Phase 8)

The model **interprets and drafts**. Code **decides and states facts**. Provider, model id and key come from
the environment (`SETUHAUL_MODEL_PROVIDER`, `SETUHAUL_MODEL_ID`, `SETUHAUL_MODEL_API_KEY`); none is in the repo.
Two provider adapters exist, `anthropic` and `openai`, chosen by `SETUHAUL_MODEL_PROVIDER`. The OpenAI adapter was run live on 2026-11-20 on the
earlier numbered-menu flow; those recordings are retired and the current flow has no recorded live run. Provider approval is still open (Q9).

## Limits (`ModelPolicy`, provisional until approved)
| Limit | Value | Enforced by |
|---|---|---|
| Temperature / output cap | 0.0 / 800 tokens | adapter request |
| Call timeout / retry | 8 s / one retry on transient error | adapter + `complete_with_retry` |
| Rounds / tool executions per driver message | 6 / 4 (rejected calls do not count) | guard code in the graph nodes |
| Request deadline | 30 s (injectable clock) | loop guard |
| Cost | hard stop at USD 0.05 per message; prices come from env, unset means cost is never invented | loop guard |
| Failure handling | transient x2 -> `MODEL_TIMEOUT` / `MODEL_TRANSIENT_FAILURE`; permanent -> `MODEL_PERMANENT_FAILURE`; all -> `ModelUnavailable` | gateway |

The loop is a LangGraph state graph (decision D13). Its recursion limit is only a backstop: a mutation test that
removes the round guard is stopped by the backstop, and the cap tests fail, proving the guards are what enforce it.
External tracing (LangSmith) is OFF unless `SETUHAUL_LANGSMITH_ENABLED=true` (default false), `SETUHAUL_LANGSMITH=approved` and `LANGSMITH_API_KEY` are all set (`langsmith_trace.py`). Global LangChain tracing stays forced off; an explicit tracer is attached whose client scrubs inputs, outputs and metadata (phones, emails, secrets and secret-named keys; `SETUHAUL_LANGSMITH_HIDE_TEXT=1` hides all message text). A tracing failure never breaks a turn. Never enabled in tests, the load profile or replay.

## What the model can call
Read tools: `list_my_shipments`, `get_shipment_status`, `compute_eta`, `search_slot_options`, `compare_option_set`,
`offer_original_slot`. Write tools: `request_hold`, `confirm_reschedule`, `release_pending_change`, `decline_held_slot`,
`cancel_appointment`, `request_partner_slot` (offered only when one of the driver's open loads is bound for a partner
warehouse; it records an email request, books nothing, and the reply is worded by the system from the stored request).
Always: `escalate_to_operations` (SAFETY, REGULATED_LOAD, POLICY_CONFLICT, DRIVER_REQUESTED,
OTHER_UNCERTAINTY). Each description is written as a job description: USE / DO NOT USE / ARGUMENTS / RETURNS / FAILURE CODES.

Rules enforced in code, not by the prompt:
1. No tool has a driver or thread parameter; identity comes from the channel/session. Extra fields are rejected.
2. The offered set depends on state: `request_hold` and `compare_option_set` need a stored option set;
   `confirm_reschedule` needs a live hold created in an **earlier** message; `release_pending_change` needs a hold.
   A call to an unoffered tool returns `TOOL_NOT_ALLOWED` and runs nothing.
3. Write checkpoint: `driver_quote` must appear (case/space-insensitive) in the driver's **current** message
   (`APPROVAL_NOT_PRESENT` otherwise); at most one committed write per message (`ONE_WRITE_PER_MESSAGE`);
   only the latest option set can be held (`NOT_LATEST_OPTION_SET`).
4. The model never supplies an ETA: `compute_eta` / `search_slot_options` derive it from a delay report (planned ETA baseline).
5. Text from loads, notes or other parties is returned only under `untrusted_text`.

## What the system words itself
A committed hold, confirmation, release or cancellation is worded from the committed row in the driver's language
(English, Hindi, Hinglish); the model's text is not used for that turn. Model text is blocked and replaced if it
(a) contains any substantial line of the system prompt or the canary, (b) claims a hold with no live hold,
(c) claims a confirmation without a confirmation this turn or a status read, and a status-backed claim may name only
times the tool returned. A lapsed hold is reported once from the `HOLD_EXPIRED` event.

## Failures
| Situation | Driver hears | Recorded |
|---|---|---|
| Model fails before any commit | safe status, nothing booked or held | `MODEL_UNAVAILABLE` escalation |
| Model fails after a commit | the deterministic committed-state wording | nothing |
| Round / tool / deadline / budget limit | hand-off wording with case reference | `MODEL_LOOP_LIMIT` |
| Database fails mid-turn | temporary-unavailable, nothing promised | `DATABASE_UNAVAILABLE` in the JSONL fallback (a DB outage cannot be recorded in the DB) |
| No slot, missing labour/consignee evidence, approval needed, ETA contradiction, cancellation cutoff | hand-off wording | reason code plus eta, rejection counts, missing inputs, recent turns, tools run |
| Unsupported language | one bilingual language question, then hand-off | `LANGUAGE_UNSUPPORTED` |

## Known limits
Language detection and the claim guard are heuristics (script ratios, word lists, regexes), not a classifier: they can
miss a paraphrased claim or misread a short message. Hindi and Hinglish wording was written by the assistant and **needs
review by a native speaker** before any driver sees it. Automated tests use mock transports only.
