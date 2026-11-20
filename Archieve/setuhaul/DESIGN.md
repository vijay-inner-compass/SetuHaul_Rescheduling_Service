# SetuHaul exception service: architecture built on the Sameer-session concepts

One rule from every session: **the model proposes, your code decides.**
Identity comes from the token, writes are guarded in code, and each fault is found from a trace before it is fixed.

## 1. Problem framing (S5: four moves)

| Move | SetuHaul answer |
|---|---|
| Define | 15-25 delay messages a day, 20-35 in a 30-minute spike, 5 coordinators, no GPS. Many drivers contend for few compatible dock slots. |
| Outcome | Share of delayed trucks given a *correct, still-valid* slot without a coordinator. Guard rails: 0 double bookings, 0 options found infeasible at confirmation, every no-slot case escalated with a full record. |
| Deconstruct | The unit is one driver's journey: report delay -> clarify -> options -> choose -> confirm (or escalate). Only the dock step uses scarce capacity. |
| Alternatives | A form fails on "around two hours late". The LLM goes only where language is messy. Capacity is a structured decision. |

Hypothesis: an agent that asks only for missing facts and never reserves anything by itself resolves most cases in 3-4 turns.
Trade-offs to guard (S5 "trade-offs", S11): wrong promise, stale option, cross-driver leak, false "confirmed".

## 2. Architecture (S1, S2, S5: ten single-purpose files)

```
Driver chat (WhatsApp/app)
   |  token + message
   v
app.py  (driver: 5 calls)      <- only receives the request and runs the steps in order
   1 sessions.require_driver(token)      token -> driver_id (never a model argument)
   2 guardrails.pre_input(msg)           deterministic safety/regulated triggers -> escalate, no LLM
   3 memory.load_history(driver, thread)
   4 agent.run(...)                      model + tool loop, cap 6 rounds
   5 guardrails.post_output + memory.save -> reply
   |
agent.py  (model + tool loop; S1/S2)  ---- tools.py (describe + call; built per request from token)
                                            |
                                            v
                    capacity.py  <-- ONLY module that writes appointments (code decides)
                                            |
                                         database.py (all SQL, BEGIN IMMEDIATE)
                                            |
                                     SQLite (the "system of record")

Side modules:  escalation.py (hand-off packet) · observability.py (request id, spans) · evals/ · tests/
```

File split by reason to change (S5 test: "is it core to the driver's role? can it be reused?"):

| File | Reason to change | Course source |
|---|---|---|
| `app.py` | request flow | S2, S5 |
| `sessions.py` | auth scheme (token, later JWT) | S4, S13 |
| `memory.py` | history store (SQLite now, Redis/Postgres later) | S4 |
| `agent.py` | model, prompt, loop | S1, S2 |
| `tools.py` | what the model may ask for | S1, S6 |
| `models.py` | argument shapes (Pydantic `args_schema`) | S6 |
| `capacity.py` | feasibility, holds, priority policy | S7, S11 |
| `database.py` | SQL and transactions | S6, S7 |
| `guardrails.py` | live controls | S11 |
| `escalation.py` | hand-off record | S11 |
| `observability.py` | spans, request ID | S8, S9 |

## 3. What the model decides vs what code decides

| LLM (edges) | Code (truth) |
|---|---|
| Parse "around two hours late", "anything after 7", "the second one works but I leave by 9" into tool arguments | Whether a slot is feasible (dock compatibility, ETA + buffer, not blocked, not taken) |
| Ask only for facts the backend does not hold | Whether two drivers can be promised the same slot |
| Explain options and refusals in plain words | Hold, confirm, release, expiry |
| Compare options across turns | Priority ranking when capacity is short |
| | Whether a booking actually committed (read back from the table) |

Anti-pattern from S1: never hand the model the whole schedule to "analyse". It calls `find_options`.

## 4. Tools (S1, S2, S6): public vs private, no ID arguments

| Tool | Reads/writes | Notes |
|---|---|---|
| `get_my_shipments()` | read | Driver comes from the token. Resolves "which load?" when a driver has two shipments (decision 2). |
| `report_delay(shipment_id, delay_minutes \| new_eta, source)` | write ETA | Validated by Pydantic. Records source and confidence. A repair time is not an arrival time (decision 3). Delays over 24 h go to a human. |
| `find_options(shipment_id, not_before?, limit)` | read only | **Showing is not reserving.** Nothing is written. |
| `hold_option(shipment_id, slot_id, idempotency_key)` | write | Takes a 10-minute hold in one transaction. |
| `confirm_booking(hold_id)` | write | Re-validates everything. Success is read back from the table. |
| `release_hold(hold_id)` | write | Driver changes their mind. |
| `escalate_to_human(shipment_id, reason)` | write | Writes the hand-off packet. |

Rules carried over from the course:
- **Identity (S2, S5 lock 1):** no tool takes `driver_id`. The token travels in the run config and `require_driver(token)` resolves it. Ownership is rechecked per shipment (`get_owned_shipment`, S7).
- **Dangerous writes (S11):** the model only *signals* "confirm". Code checks owner, state, feasibility and expiry, then executes. There is no cancel-appointment or reassign tool bound to the model.
- **Schema on the tool, not the model (S6):** keeps `tool_calls` so the loop continues. Each layer checks what it can know: Pydantic checks shape, the database checks facts, the driver supplies intent.
- **Idempotency (S1 drill, S2):** one key per driver *action*, reused on retries. A duplicate message returns the original result.

## 5. Capacity layer: the state model (decisions 5, 6, 9, 13)

```
shown (nothing written) -> held (TTL 10 min) -> confirmed
                           held -> released | expired      confirmed -> released (on swap)
```

| State | Who moves it | What the driver sees |
|---|---|---|
| shown | `find_options` (read only) | "These slots look possible right now." |
| held | `hold_option`, after the driver says yes | "Kept for you until 14:35." |
| confirmed | `confirm_booking`, after a second yes | "Confirmed: Dock 3, 15:00." |
| released / expired | driver, timeout, or swap | "That hold lapsed; here are fresh options." |

"Available" while another driver decides = feasible **and** not confirmed **and** not under a live hold. Expiry is judged by timestamp, so the read path never writes.

Database-enforced invariants (S7 partial unique index; extends the two rules the brief says already exist):
- one active (held or confirmed) appointment per slot,
- one hold per shipment,
- one confirmed per shipment.

What those rules do not cover (to prove in the data audit): dock compatibility, ETA vs slot start, blocked windows, overlapping slot rows on one dock, and two facilities' rules clashing.
`check_invariants()` is the code evaluator for these and must return `[]` after any workload.

Concurrency (S7): `BEGIN IMMEDIATE` takes the write lock first, so check-then-write cannot interleave. The unique index is the last defence; the race loser gets `taken` and fresh options.
Verify-list note (S13): `IMMEDIATE` can fail with *busy*, so set a connection timeout and retry.
Stale options: `confirm` re-runs the same feasibility check as `find_options`.

Priority policy (decision 7), in order:
1. Hard constraints (feasibility) are never traded away.
2. Priority class (`high` before `standard`).
3. Earliest current ETA.
4. Arrival order (stable sort).
5. Never pre-empt a held or confirmed slot. Re-ranking applies only to unallocated capacity.

## 6. Memory and state (S3, S4)

- Three IDs kept apart: **token** (proves login), **driver_id** (stable), **thread_id** (one conversation). Key history by `(driver_id, thread_id)`, never by token.
- A driver with two shipments in one chat is resolved by `get_my_shipments` and a clarifying question, not by guessing.
- Store: SQLite `messages` for the prototype. Production is Postgres for the durable record plus Redis for recent messages with a TTL (S4 table). Remember the LPUSH order trap.
- Limits: cap history at about 20 messages and summarise the oldest.
- The booking state is **not** in chat history. It lives in `appointments`, the one true copy, because a model can forget (S7 draft rule). The agent re-reads state with `get_my_shipments` each turn.

## 7. Guardrails (S11: eight failure types, three placements)

| Failure | Placement | Control | Method |
|---|---|---|---|
| Another driver's data (leak) | tool/action | no ID argument; ownership check | authorization |
| Prompt injection via driver text or load notes | input -> action | remove capabilities; no dangerous tool bound | allowlisting |
| Unauthorized tool | action | never bind release-others / reassign | allowlisting, human in the loop |
| Invalid arguments (delay -5, 9999 minutes) | action | Pydantic `Field(ge, le, strict)` | schema validation |
| Business policy (delay > 24 h, regulated load) | action | if-else before the write -> escalate | rule-based |
| Malformed output | output | validate shape; retry once or reject | schema |
| Unsafe tone | output | keyword then small check | classifier (last) |
| **False confirmation** ("you're booked" with no booking) | output/flow | reply may say "confirmed" only if `confirm_booking` returned `ok` this turn | verified-state check |

Cheapest first (S11): code -> configuration -> regex -> LLM last. Pre-input safety words (accident, injury, hazmat, fire) escalate without calling the model.

## 8. Escalation (decisions 10-12)

A human takes over when: no feasible slot, contradictory data (gate-in vs ETA), regulated load, safety/emergency, penalty or compensation talk, repeated tool failure, delay over 24 h.
Hand-off packet: driver, shipment, ETA history with sources, options shown, holds and their states, conversation summary, reason code, and a suggested next step.
Tell the driver plainly that a coordinator will contact them, and promise nothing.

## 9. Observability and evals (S8-S10)

- **Instrumentation (S8):** request ID on every log line; spans: `chat` (root) -> `model.invoke` -> `tool.<name>` -> `capacity.hold_slot`. Never log tokens or phone numbers.
- **Three problem types:** operational (latency, errors, cost), application-specific (wrong tool, wrong ETA), rule broken (double booking, policy).
- **Vitals (S9):** P50/P99 per trace, tool calls per trace, error rate. The five groups: Input, Trajectory, Decision, Dependency, Technical outcome.
- **True success rate** (the "79% -> 58%" lesson): count outcomes (valid confirmed booking or correct escalation), not HTTP 200s.
- **Evals start from failures (S10):** run scripted scenarios, read the traces, group failures, then derive checks.
  - *Code evaluators:* `check_invariants()`; "hold came before confirm"; "no `confirm` reply without `ok`"; options re-validated at confirmation.
  - *LLM judge (small model, binary rubric):* did the reply avoid overpromising, and were only missing facts asked?
- **Brief metrics mapped:** turns and elapsed time per scenario; share without takeover; duplicates (integrity test); options later infeasible (re-check at confirm); no-slot escalations with complete packets; gate-in to dock-in wait; slot utilisation; priority-policy violations; clarification turns per case.

## 10. Tests and proof (S7: one piece, one test)

1. `test_integrity`: N threads (10 drivers) race for one slot -> exactly 1 winner, `check_invariants() == []`.
2. `test_stale`: option shown, another driver takes it, hold/confirm refuses with a reason.
3. `test_expiry`: hold lapses; confirm fails; slot reopens for others.
4. `test_idempotent`: duplicate message and retried hold -> one row.
5. `test_identity`: a foreign `shipment_id` returns nothing.
6. `test_policy`: competing trucks follow the stated order; no pre-emption.
7. `test_scenarios`: scripted-model run of the seven demo items, ending in the no-slot escalation (SHP1015 / THR005 / OM004 case).
8. `test_false_confirmation`: a model that lies "confirmed" is blocked by the guard.

A **scripted model** stands in for the LLM so tests are deterministic and free; the real model is swapped in for the recorded run.

## 11. Deployment path (S12, S13), when needed

The loop does not change; only where it runs and who may call it.
- Entry file `runtime.py`: FastAPI with `/ping` and `/invocations` on port 8080, zipped for Linux ARM64 with the libraries. Secrets and the SQLite file stay out of the zip.
- Role: trust policy for the AgentCore service plus a minimal permission policy; cut statements the workload does not use.
- Inbound auth: IAM for internal use; **JWT from the client's identity provider** for a driver-facing chat.
- The backend holds the IAM user and calls the runtime by ARN; the browser never holds AWS credentials. Keep the customer -> session ID map and pass the **session ID down to the tools**. A client-supplied session ID is not authorization.
- Runtime memory ends with the session, so move history and state to external stores (Redis/Postgres).
- Debug order: CloudWatch log first; then missing env var, Python 3.12 vs 3.13, bucket region, model-name typo. Check current AWS docs, since class and docs differ on Python version, size and role statements. Delete the runtime and access keys after practice.

## 12. Decision register (first drafts of the 13)

| # | Draft answer |
|---|---|
| 1 | Ask for: which shipment (only if two exist), delay as minutes **or** a clock time, whether the figure includes repair time, earliest time they can reach the dock, any hard departure time. Never ask for what the backend holds (vehicle, facility, appointment). |
| 2 | Token -> driver_id -> open shipments. If one, assume it and say so. If more than one, ask. Trace the message to rows in the audit. |
| 3 | Revised ETA = now + declared delay, or the stated clock time. Source and confidence are stored. A repair estimate is flagged lower confidence. Over 24 h escalates. |
| 4 | Feasible = same facility, compatible dock, not blocked, starts at least 15 min after ETA, not taken or held, not in the past. Tested on three shipments from the ladder. |
| 5 | Available = feasible and not confirmed and not under a live hold. State needed: `appointments.status` + `hold_expires_at`. |
| 6 | shown -> held (code, after driver yes) -> confirmed (code, after second yes). Driver sees the wording in section 5. |
| 7 | Policy in section 5; tested on SHP1006/1012/1013/1014/1009. |
| 8 | (Optional) Recalculate on ETA change or gate-in; fixed work = trucks already unloading and confirmed promises; objective = least waiting, fewest changed promises. |
| 9 | Idempotency key per action; expiry timestamp; `confirm` re-validates; cancellation = release. One replay each. |
| 10 | Escalate with the packet in section 8; no invented slot. |
| 11 | Plain refusals that state the reason and the next step ("that dock cannot take a refrigerated load; here are two that can"). |
| 12 | Safety, penalties/compensation, regulated loads, contradictory data, no-slot, repeated failure. |
| 13 | Concurrency test (section 10, test 1) with its result in the README. |

## 13. Open items

- The supplied SQLite file is not available yet. The prototype schema is synthetic, so `database.py` is the single place to adapt to the 18 real tables.
- The data audit (Problem Definition Sheet) needs that file: each issue is proved with a query.
- Confirm model and library versions for the README when building.
