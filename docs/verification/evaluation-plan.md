# Evaluation plan (Phase 10)

## Status of the live run: BLOCKED
The plan asks for at least one real-model scenario. **None has been run.** No provider, model id or key is configured
(decision Q9 is unanswered) and I will not invent a trace. Everything that does not need a model is built and tested:
`scripts/run_live_model_scenario.py` exits with code 3 and calls nothing until `SETUHAUL_MODEL_PROVIDER`,
`SETUHAUL_MODEL_ID` and `SETUHAUL_MODEL_API_KEY` are set in the environment. When they are, it runs a four-message
scenario (repair with no travel time -> travel time -> pick the first option -> confirm), writes the raw trace to
`live_traces/` (git-ignored) and a sanitised copy to `tests/traces/live-<id>.json`, and prints the evaluator results and
failure classes. **A person must read that whole trace** before any evaluation is derived from it or any threshold is judged.

## What exists
| Piece | Where | Notes |
|---|---|---|
| Trace model and recorder | `evaluation.py` (`Trace`, `RecordingGateway`) | Works on live and scripted gateways. Each trace says `source: live` or `scripted`. |
| Sanitiser | `sanitize_trace` | Run-specific ids become `$OPTn` / `$HOLDn` / `$CASEn`; phone numbers, emails and secret-looking strings are redacted; ISO dates are kept. Driver ids in the snapshot are synthetic and kept. Free text is kept except those patterns: a real driver's message needs a human review before commit. |
| Failure taxonomy | `classify_failure` | INPUT (language), TRAJECTORY (disallowed tool, caps), DECISION (no approval, wrong option set, foreign ids), DEPENDENCY (model/database down), TECHNICAL_OUTCOME (invalid arguments), INSUFFICIENT_EVIDENCE (empty reply). Empty list = nothing went wrong. |
| Deterministic evaluators | `eval_*` | identity, tool order, explicit approval, option-set ownership, state-backed wording, round and tool caps, hand-off completeness. Allocation invariants are checked on the replay database. |
| Replay | `replay_trace`, `scripts/replay_traces.py` | Feeds the *recorded model outputs* through the real deterministic layers on a fresh database and compares tool results, replies, hold/confirm state and invariants. A test forbids any HTTP call during replay. |
| Baselines | `tests/traces/*.json` (5) | **Scripted** regression baselines recorded by `scripts/record_scripted_traces.py`: clarify/options/hold/confirm, no-slot hand-off, prompt injection, Hinglish hold expiry, never-mind release. They show the deterministic layers still behave; they say nothing about any model. |

## Replay result today
`python scripts/replay_traces.py`: 5/5 traces replay clean, 56/56 evaluator checks pass, 0 mismatches, 0 invariant violations
(`docs/verification/phase10-replay.txt`). The evaluators can fail: tests plant a bad trace for each one, and replay is
shown to catch a changed ranking rule, a changed checkpoint rule and a doctored recorded result.

## Thresholds from Phase 0 against what was measured
| Threshold | Measured | Numerator / denominator | Verdict |
|---|---|---|---|
| Zero conflicting allocations | Phase 7 proof, 20 runs x 5 scenarios plus property tests | 0 violations / 100 contention runs | met on the prototype (SQLite, one machine) |
| Zero unauthorised cross-driver reads | foreign shipment, option set, hold and case ids refused in tests and in the injection baseline | 1 attempt / 1 refused in the baseline traces; 0 successful in all tests | met in tests; not measured on real traffic |
| 100% state-backed confirmations | baseline replies that claim a confirmation | 1 / 1 backed by a committed row; holds 3 / 3 backed by a live hold | met on baselines only |
| 100% complete no-slot hand-offs | no-slot baseline | 1 / 1 carried reason, eta, rejections, recent turns, tools run | met on one case |
| Median <= 2 clarification turns | baseline has 1 scripted clarification | n = 1, scripted | **not measured**: a script is not a model |
| >= 80% automatic resolution on the approved safe set | no approved safe scenario set exists and no live run | n/a | **not measured** |
| Capacity search p95 < 500 ms; stub-model API p95 < 2 s | search test asserts the bound; the API does not exist yet | n/a | measured properly in Phase 11 |

No threshold was changed. Nothing above supports a claim about model quality, language quality or operational success.

## What to do when a key is available
1. Set the three environment variables (and the price variables so the cost stop works); run the live script once.
2. Read the entire raw trace; group each problem with the taxonomy; note anything the evaluators did not see.
3. Review the sanitised copy for personal data the patterns missed, then commit it.
4. Add an evaluator only for a failure the trace actually showed (and a planted-fault test for it).
5. Re-run `scripts/replay_traces.py`; compare against the thresholds and record any change with the evidence for it.
