# Operations Questions (Phase 0)

Every value below is an **engineering default, not a fact from the brief**. Status stays `PROPOSED` until the approving role replies. Unanswered items are carried as explicit approved-default-or-blocker.

| # | Question | Provisional default | Consequence if wrong | Approving role | Status |
|---|---|---|---|---|---|
| Q1 | Hold TTL | 3 minutes | Too short: drivers lose holds mid-reply. Too long: capacity idles during spikes. | Warehouse operations lead | PROPOSED |
| Q2 | Expiry sweep interval | 30 s (prototype); external scheduler in production | Late sweeps keep dead holds blocking slots. | Engineering lead | PROPOSED |
| Q3 | ETA buffer before dock release | None approved (blocker for Phase 4 tuning; Phase 2 uses 0) | Trucks offered slots they cannot reach. | Warehouse operations lead | BLOCKER (default 0 min) |
| Q4 | Priority classes | Not defined in brief; priority is advisory only, never pre-empts a hold | Wrong ordering in ranking only. | Operations manager | BLOCKER (default: advisory) |
| Q5 | Simultaneous-request policy | FCFS at successful hold commit, after hard eligibility checks | A high-priority truck can lose to an earlier low-priority one. Documented trade-off. | Operations manager | PROPOSED |
| Q6 | Cancellation cutoff | Not defined in brief (blocker for Phase 6) | Cancellations too close to slot time wrongly accepted/refused. | Operations manager | BLOCKER |
| Q7 | Override roles | Coordinator may claim and resolve; no role may bypass hold/confirm checks | Unauthorised overrides, or coordinators blocked. | Operations manager | PROPOSED |
| Q8 | Notification recipients | Warehouse planner, consignee, operations | Missed downstream parties. | Operations manager | PROPOSED |
| Q9 | Model provider / model ID | Provider-neutral gateway; set by `SETUHAUL_MODEL_PROVIDER` / `SETUHAUL_MODEL_ID` env vars, no secret in repo | Phase 8 and 10 cannot run live. | Engineering lead + data protection | BLOCKER (needed by Phase 8) |
| Q10 | Language policy | English, Hindi, Hinglish; otherwise one language-choice question then escalate `LANGUAGE_UNSUPPORTED` | Regional-language drivers get escalations. | Operations manager | PROPOSED |
| Q11 | Rate limits | 20 msg/min, 100 msg/hour per driver, configurable per channel | Abuse, or legitimate bursts blocked. | Engineering lead | PROPOSED |
| Q12 | Acceptance thresholds | Zero conflicting allocations; zero cross-driver reads; 100% state-backed confirmations; 100% complete no-slot hand-offs; median <= 2 clarification turns; >= 80% auto-resolution on approved safe set; capacity search p95 < 500 ms; stub-model API p95 < 2 s | Claims of success not comparable to what ops expects. | Operations manager | PROPOSED |
| Q13 | Conversation cache | 20 turns or 12,000 chars; 24 h Redis TTL | Lost context vs. memory cost. | Engineering lead | PROPOSED |
| Q14 | Option-set retention | 30 days | Cannot reconcile old options / privacy exposure. | Data protection | PROPOSED |
| Q15 | Model loop and cost caps | 6 rounds, 4 tool executions, 8 s call timeout, 1 retry, 30 s deadline, 800 output tokens, USD 0.05/message alert | Runaway cost or latency. | Engineering lead | PROPOSED |
| Q16 | Escalation SLA | None; driver is never promised a response time | Driver expectations. | Operations manager | PROPOSED |
| Q17 | Source of labour and equipment availability (crews per facility per time window) | None exists in the snapshot. Tables `labour_windows` and `consignee_commitments` start empty, so every slot returns `INCOMPLETE_FEASIBILITY_DATA` and the case is handed to a human. Test and demo rows are labelled `TEST_FIXTURE`. | Without a real feed the service can never auto-confirm. | Warehouse operations lead | BLOCKER (needed for any automatic resolution) |
| Q18 | How fresh must facility state be before offering capacity | 5 minutes | Stale state offers slots that are gone, or refuses good ones. | Engineering lead | PROPOSED |
| Q19 | How long may a shown option be selected | 15 minutes (then re-search). Holds still last 3 minutes. | Too short: drivers return to expired options. Too long: shown options drift from reality. | Operations manager | PROPOSED |
| Q20 | How to book an unload longer than the slot (SHP1009 and SHP1014: 75 min vs 60 min slots; the supplied appointments already book them into 60-minute slots) | Strict: the unload must fit one slot, otherwise no option and a hand-off. | Strict rule sends two CRITICAL loads to humans. Allowing overrun risks double-booking the next slot unless multi-slot holds are built. | Warehouse operations lead | BLOCKER for automatic handling of these two loads |
