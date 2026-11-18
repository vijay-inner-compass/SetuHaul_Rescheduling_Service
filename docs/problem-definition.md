# Problem Definition

**Problem.** When a driver reports a delay, early arrival or breakdown, a warehouse planner must find a new dock slot by hand. Capacity is contested during spikes and drivers receive inconsistent promises.

**Service goal.** A multilingual conversational service that (1) derives the driver's ETA from evidence, (2) shows only feasible options and records what was shown, (3) holds capacity safely, (4) confirms only from committed state, and (5) hands over to a human with full context whenever it cannot act safely.

**Division of labour.** The model interprets English, Hindi and Hinglish text and calls narrow read tools. Deterministic Python services own identity, ETA derivation, feasibility, allocation, holds, expiry, cancellation, idempotency, notifications and escalation.

**Out of scope until Phases 0-12 are approved:** facility-wide optimisation (Phase 13).

**Not provable from the snapshot:** see `snapshot-limitations.md`.
