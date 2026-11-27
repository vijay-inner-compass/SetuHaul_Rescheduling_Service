# Readiness detail (for engineers)

The one-page note for operations is [client-readout.md](client-readout.md).

## In one paragraph
A driver messages in English, Hindi or Hinglish; the service works out the real arrival time, holds one feasible dock slot for a few
minutes, confirms it atomically when the driver says yes, and queues notices for the warehouse planner, the consignee and operations.
When it cannot act safely it opens a case for a coordinator instead of guessing. Drivers use Telegram; warehouse staff, gate security
and admins have signed-in web screens. The booking rules are enforced by the database and plain code, not by the model, and they held under 20
repeated rounds of contention. **What has not happened:** a real model has only single recorded runs, made before the 2026-11-27 changes, no real driver
has used it, and the supplied snapshot has no labour or consignee data, so for the supplied loads every slot decision goes to a person
(loads created on the warehouse screen carry typed crew numbers and deadlines).
Questions an audience may ask are answered in [faq.md](faq.md). Updated 2026-11-28.

## The six deliverables
| # | Deliverable | Where |
|---|---|---|
| 1 | Problem definition and data audit | `docs/problem-definition.md`, `docs/data-audit.md`, `docs/verification/data-issues.md`, `docs/snapshot-limitations.md` |
| 2 | Decision register | `docs/decision-answers.md` (the 13 decisions), `docs/decision-register.md` (D1 onward), `docs/operations-questions.md` (Q1-Q20) |
| 3 | Working prototype | `src/`, `migrations/`, `README.md` (about 1,970 tests) |
| 4 | Scenario run | `docs/verification/scenario-run.md` (`python scripts/run_scenario.py`, 23 checks) |
| 5 | Integrity proof | `docs/verification/concurrency-proof.md`, `docs/verification/scale-test.md` |
| 6 | Client readout | `docs/client-readout.md` |

## What the evidence supports
| Claim | Evidence | Strength |
|---|---|---|
| No two requests can be given the same slot; at most one hold per shipment | 20 rounds of 10 threads, 6 processes and mixed contention, 0 invariant violations; Hypothesis sequences with external inserts; planted-fault and mutation tests | strong, for SQLite on one machine |
| "N minutes late" counts from the planned ETA; a repair time alone is not an ETA; a gate-in contradicts a delay report | unit tests and the scenario | strong |
| Held and confirmed wording always matches stored state | wording is rendered from committed rows; a guard blocks unbacked claims; 66/66 evaluator checks on the scenario | strong for the deterministic path, heuristic for model free text |
| Safe under prompt injection and foreign ids | allowlist by state, write checkpoints, no identity parameter, refusals recorded | strong in tests; the injection case has not been run against a real model |
| Failures are safe | model, database, lock and notification failure paths each tested | strong |
| Performance | capacity search p95 12.9 ms; stub-model API p95 61 ms; hold write p95 4.4 ms; about 245 holds/s | real-model latency and multi-worker not measured |

## Acceptance thresholds, with numerators
| Threshold | Measured | Verdict |
|---|---|---|
| Zero conflicting allocations | 0 violations in 100 contention runs and the scenario | met (prototype scope) |
| Zero unauthorised cross-driver reads | 0 succeeded of 1 scripted attempt in the scenario, 0 in all tests | met in tests only |
| 100% state-backed confirmations | 1 / 1 in the scenario, 1 / 1 in baseline traces | met on scripted runs only |
| 100% complete no-slot hand-offs | 3 / 3 hand-offs in the scenario | met |
| Median at most 2 clarification turns | not judged: one scripted clarification | **not measured** |
| At least 80% automatic resolution on the approved safe set | no approved set; three single live runs are not one | **not measured** |
| Capacity search p95 below 500 ms; stub-model API p95 below 2 s | 12.9 ms; 61 ms | met, but the API figure excludes a real model |

Nothing here supports a claim of operational success: the snapshot is 21 shipments on one day.

## Findings about the data
- **No labour or consignee data exists.** Every slot that passes the hard rules comes back "cannot confirm availability", so
  nothing can be auto-booked on the snapshot until a labour feed exists (Q17).
- **SHP1014 (21,000 kg, 75 min) and SHP1009 (75 min) have no single-slot option:** standard slots are 60 minutes and SHP1014 is also too
  heavy for two docks. The snapshot itself books 75-minute loads into 60-minute slots, so the strict rule is stricter than current practice (Q20).
- Supplied oddities the service handles rather than assumes away: a duplicate exception row, an exception with no shipment, a duplicate chat
  message, and a slot with a pending (not confirmed) appointment.

## Open risks, most important first
1. **Thin live-model evidence.** OpenAI `gpt-6-luna` ran one scenario each in English, Hindi and Hinglish on 2026-11-26 ([evaluation-plan.md](verification/evaluation-plan.md)); in the English run it handed off a message it should have asked about. Nothing is recorded since the 2026-11-27 changes. Model quality, Hindi and Hinglish quality and the two model-dependent thresholds are unmeasured. Data protection for real driver messages (Q9) is undecided, and there is no retention, export or erasure of personal data.
2. **Hindi and Hinglish wording is unreviewed** and language detection and the claim guard are heuristics (word lists, regexes).
3. **Labour and consignee evidence has no source** (Q17); the cancellation cutoff (Q6), ETA buffer (Q3) and priority classes (Q4) are defaults.
4. **Channels are minimal:** Telegram was tried once with the stand-in assistant; warehouse, consignee and operations notices are queued with no sender (messages to drivers are sent); coordinator service calls use bearer tokens.
   The real messaging provider's signature format and delivery are not integrated.
5. **SQLite limits:** about 245 writes/s and multi-second lock waits from four writers up (see below).
6. **GitHub Actions is not used** (D56, by choice): later commits were checked locally on Windows, and Linux only at an earlier phase.
   Ubuntu behaviour of the later phases is untested here.
7. Single-day, 21-shipment snapshot; invented demand in the scale fixture; Redis behaviour verified only with an in-process fake.

## Policies and defaults awaiting approval (`docs/operations-questions.md`)
| Area | Current default | Status |
|---|---|---|
| Hold TTL 3 min; expiry sweep 30 s; option lifetime 15 min; state freshness 5 min | in code | proposed (Q1, Q2, Q18, Q19) |
| First valid hold wins; priority never pre-empts | in code | proposed (Q5) |
| Cancel only 120+ min before the slot and with a known, penalty-free customer commitment | in code | needs approval (Q6) |
| Unload must fit one slot | in code | needs approval (Q20) |
| Escalation: reopen window 24 h, no response time promised | in code | proposed (Q16) |
| Rate limits 20/min and 100/h; language policy English, Hindi, Hinglish | in code | proposed (Q10, Q11) |
| Model provider, model id, data protection | OpenAI `gpt-6-luna` used for test runs only; data protection undecided | **blocker for real traffic** (Q9) |
| Labour and consignee data source | typed by warehouse staff for loads created on the screen; no feed; none for supplied loads | **blocker** (Q17) |
| ETA buffer, priority classes | 0 min, advisory only | blockers for tuning (Q3, Q4) |

## When to move to PostgreSQL
Before any of: more than one sustained writer service; more than four API workers; a lock-timeout rate above 1% or a p95 write transaction
above 250 ms under the approved load profile; or lock waits (p99 or maximum) approaching the 5 s busy timeout, which the saturation test already shows from four writers.
The booking invariants (partial unique indexes, one transaction per change) carry over; the driver and locking calls change.

## Recommended next steps
1. Approve the provider and data-protection position (Q9), supply a key, and run the live scenario; read the trace; add evaluations only for failures it shows.
2. Name the labour and consignee data source (Q17); until then supplied loads reach a coordinator and screen-created loads rest on typed figures.
3. Decide Q6, Q20, Q3 and Q4; have a native speaker review the Hindi and Hinglish text.
4. Decide the personal-data position (notice, retention, erasure), host behind HTTPS with backups and monitoring, and connect senders for warehouse and consignee notices; run and check the first deployment.
5. Record new real-model runs after the 2026-11-27 changes and have them reviewed.
