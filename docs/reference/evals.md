---
title: Evals
type: reference
created: 2026-11-28
updated: 2026-11-28
sources:
  - code: src/setuhaul/evaluation.py
  - code: src/setuhaul/integrity.py
  - code: src/setuhaul/proof.py
  - code: scripts/replay_traces.py
  - code: scripts/record_scripted_traces.py
  - code: scripts/run_live_model_scenario.py
  - code: scripts/run_scenario.py
  - code: scripts/run_integrity_proof.py
  - code: scripts/run_scale_profile.py
  - code: tests/test_trace_replay.py
  - code: tests/traces
verified_at: 8a5487e
---

# Evals

What is checked about the service's behaviour, and what each check can and cannot show. The controls being checked are in
[[guardrails]]. History and acceptance thresholds: [evaluation-plan.md](../verification/evaluation-plan.md) and
[readiness-detail.md](../readiness-detail.md).

## Kinds of evidence

| Eval | Run with | Calls a model | Shows |
|---|---|---|---|
| Trace replay with evaluators | `python scripts/replay_traces.py` | no | the code still behaves the same on recorded model outputs |
| Automated tests | `python -m pytest -q` | no (scripted and mock transports) | rules and failure paths |
| End-to-end scenario | `python scripts/run_scenario.py` (rewrites `docs/verification/scenario-run.md`) | no (script) | a whole conversation flow |
| Concurrency proof | `python scripts/run_integrity_proof.py` | no | no double allocation under contention, on SQLite |
| Scale profile | `python scripts/run_scale_profile.py` | no (stand-in) | latency without the model |
| Live scenario | `python scripts/run_live_model_scenario.py` | **yes**, paid | one real conversation, recorded as a trace |

Tests and replay are run locally; `scripts/ci.py` runs the local checks.

## Traces

A trace records turns, model rounds, tool calls and replies, and is labelled `live` or `scripted`
(`src/setuhaul/evaluation.py:100`). `sanitize_trace` (`:266`) swaps run-specific ids for placeholders and masks emails,
secrets and phone numbers.

| Traces in `tests/traces/` | Source | Scenario |
|---|---|---|
| `clarify-options-hold-confirm.json` | scripted | clarify, hold, confirm |
| `no-slot-handoff.json` | scripted | no slot, hand-off |
| `injection-attempt.json` | scripted | prompt injection |
| `hinglish-hold-expiry.json` | scripted | Hinglish hold lapses |
| `never-mind-release.json` | scripted | release a hold |
| `live-53f1f2f2.json`, `live-ccbe3af8.json`, `live-d9762e12.json` | live | English, Hinglish, Hindi |
| `live-f9c78531.json` | live | provider rejects the request |

What the live runs showed is in [evaluation-plan.md](../verification/evaluation-plan.md). `tests/traces/retired/` is not
replayed.

> ⚠️ The sanitiser does not mask names or other free text. A trace made from a real driver's messages needs a human read
> before it is committed.

## The seven evaluators

Plain functions over a trace, no model grading (`src/setuhaul/evaluation.py:340`).

| Evaluator | Fails when |
|---|---|
| `identity` | a call carrying `driver_id` or `thread_id` was not rejected |
| `tool_order` | two committed writes in one message, a hold with no options shown, or a confirmation with no hold from an earlier message |
| `explicit_approval` | a committed write whose quote is not in the driver's message |
| `option_set_ownership` | a hold or compare used an option set this conversation was not shown |
| `state_backed_wording` | a reply claims a hold or confirmation that state does not back |
| `round_caps` | more rounds or tool runs than the configured limits |
| `handoff_complete` | a hand-off lacks language, tools run, recent turns or selected shipment (language and database outages excepted) |

Planted-fault tests in `tests/test_trace_replay.py` show that evaluators can fail, for example
`test_committed_write_needs_the_drivers_own_words` and `test_unbacked_booking_claims_are_flagged`.

> ⚠️ No evaluator checks that a confirmation had a clear yes or that a cancellation took two messages. Those rules are
> covered by `tests/test_write_intent.py` only.

> ⚠️ `state_backed_wording` uses the same claim patterns as the reply guard, so it cannot catch a claim the guard misses.

## Failure classes

`classify_failure` (`src/setuhaul/evaluation.py:290`) labels each turn. No label means nothing went wrong. A label is not a
failed check: a refused injection is labelled and still passes.

| Class | Meaning |
|---|---|
| `INPUT` | language question or unsupported language |
| `TRAJECTORY` | tool not allowed, second write, or a loop limit |
| `DECISION` | no approval, or a stale or foreign id |
| `DEPENDENCY` | model or database failure |
| `TECHNICAL_OUTCOME` | invalid tool arguments |
| `INSUFFICIENT_EVIDENCE` | empty reply |

## Replay

`replay_trace` (`src/setuhaul/evaluation.py:512`) feeds the recorded model outputs through the real workflow on a fresh
database. It passes only with no mismatch (reply, status, reason, hold and confirm state, tool results), no failed
evaluator and no invariant violation (`src/setuhaul/integrity.py:32`). `test_replay_never_touches_the_network` keeps it
offline; other tests show it catches a changed ranking rule, checkpoint rule or recorded result.

> ⚠️ Replay cannot judge a prompt or model change: the model's side is recorded. Only a new live run can.

> ⚠️ A deliberate change of wording or tool results shows up as a mismatch. Scripted baselines are re-recorded with
> `scripts/record_scripted_traces.py`; live traces need a new paid run, which needs approval
> (`tests/traces/retired/README.md`).

## Last results

| Eval | Result | When |
|---|---|---|
| Trace replay | 9/9 traces clean, 109/109 evaluator checks | run 2026-11-28 on `main` at `8a5487e` merged with these pages |
| Other evals | see [readiness-detail.md](../readiness-detail.md) | not re-run for this page |

## Not evaluated

* How well a model understands drivers: no scored scenario set, no repeated trials, no success rate.
* No model-graded or human-scored reply quality, including Hindi and Hinglish.
* No adversarial run against a real model.
* Median clarification turns and the share resolved without a person.
* Real-model latency.
