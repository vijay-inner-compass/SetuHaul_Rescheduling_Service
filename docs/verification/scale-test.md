# Scale test and SQLite limits (Phase 11)

Reproduce: `python scripts/run_scale_profile.py` (writes `scale-results.json` and `scale-run.log` here). The Locust profile
is `load/locustfile.py`. Raw numbers below come from that run on one Windows machine.

## What was and was not simulated
| Item | Setup | Honest limit |
|---|---|---|
| Data | `scale_fixture.py`: **generated**, not SetuHaul data. 360 loads, 826 slots, 254 active appointments (supplied rows plus `GEN-` rows; every generated row is marked and labour/consignee evidence has source `GENERATED_SCALE_FIXTURE`). | Demand shape is invented. |
| Model | `StubGateway`, a rule-based stand-in that calls the real tools. | **No model latency is included.** Real model calls (8 s timeout) will dominate end-to-end time. |
| API | one uvicorn worker, in-process `fakeredis`, fixed clock at 2026-08-04 09:35 IST, localhost HTTP | Multi-worker and a real Redis were **not** measured. |
| Spike | 35 messages in 35 s from 35 different drivers. The brief's spike is 35 messages over 30 minutes, so this is **compressed 51.4x** (1800 s / 35 s). Arrival offsets are random but seeded. | Real traffic would be far gentler per second; this stresses bursts, not duration. |
| Burst | 10 drivers shown the same best slot, all asking to hold it at the same instant | |
| Locust | same spike via `load/locustfile.py`, 1 user, 1 message/s, 35 s (34 completed) | Repeatability check, not extra load. |

## Results against the approved thresholds
| Measure | Result | Threshold | Verdict |
|---|---|---|---|
| Conflicting allocations | 0 (0 invariant violations, 0 slots with more than one hold, 10-driver burst: exactly 1 winner) | zero | met |
| Capacity search p50 / p95 / p99 | 10.8 / 12.9 / 13.4 ms (200 searches over the 826-slot fixture, in process) | p95 < 500 ms | met |
| Stub-model API p50 / p95 / p99, spike | 40.7 / 60.7 / 79.4 ms (35 requests, all HTTP 200, all REPLIED) | p95 < 2 s | met, **without a real model** |
| API during the 10-way burst | p50 111 ms, p95 and max 250 ms (10 x HTTP 200) | p95 < 2 s | met |
| Locust spike | 34 requests, 0 failures, p50 28 / p95 38 / p99 39 ms | | consistent |
| Hold write transaction p50 / p95 | 3.5 / 4.4 ms; confirm 3.5 / 5.2 ms (sequential) | trigger p95 > 250 ms | well inside |
| Lock-timeout / system-error rate | 0% (0 of 89 handled messages replied TEMPORARILY_UNAVAILABLE) | trigger > 1% | well inside |
| Errors | 0 non-200 responses in the spike and burst | | |

## Write saturation (how hard SQLite can be pushed)
One connection per writer, each looping hold -> release for 4 s (WAL, busy timeout 5000 ms):

| Writers | Hold attempts/s | p50 | p95 | p99 | Max | Lock timeouts |
|---|---|---|---|---|---|---|
| 1 | 236 | 3.6 ms | 5.0 ms | 6.8 ms | 14 ms | 0% |
| 4 | 244 | 3.6 ms | 4.9 ms | 9.4 ms | 4.0 s | 0% |
| 8 | 245 | 3.6 ms | 4.6 ms | 9.2 ms | 4.7 s | 0% |
| 16 | 244 | 3.6 ms | 4.6 ms | 4.2 s | 4.7 s | 0% |

Reading it: SQLite allows one writer at a time, so throughput stays at about 245 holds/s however many writers there are.
That is far above the brief's load (a few hundred messages per day, 35 in a spike). The warning sign is the **tail**: with 4 or
more writers some requests wait several seconds for the lock (max 4.0-4.7 s; p99 4.2 s at 16 writers). They stayed under the
5 s busy timeout here, so nothing failed, but a slightly longer queue would turn those waits into `LOCK_TIMEOUT` replies. The
p95 trigger below would not catch this; watch p99 and the maximum as well.

## Prototype limit and when to move to PostgreSQL
SQLite in WAL mode is adequate for this prototype: one machine, a few API workers, bursts in the tens. Move to PostgreSQL before
any of these happens (the plan's triggers, plus the tail watch):
1. more than one sustained writer service (anything besides the API process writing continuously);
2. more than four API workers;
3. a lock-timeout rate above 1% under the approved load profile;
4. p95 write transaction above 250 ms under the approved load profile;
5. (added) p99 or maximum lock wait approaching the busy timeout, as the 4+ writer rows above already show.
The booking invariants rely on `BEGIN IMMEDIATE` and partial unique indexes, which PostgreSQL supports (`SELECT ... FOR UPDATE`
or serialisable transactions plus the same partial unique indexes), so the move changes the driver and locking calls, not the model.
