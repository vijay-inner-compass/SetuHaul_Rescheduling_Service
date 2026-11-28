# DOC-14: Test Strategy & Test Plan

| Field | Value |
|---|---|
| **Document ID** | `DOC-14` |
| **Version** | `1.0.0` |
| **Category** | 5 — Quality & Testing |
| **Repository Commit** | `445c7f965e22e7915238cca5631e68b4462d7b7b` |
| **Validation Status** | Verified against `tests/`, `scripts/`, `load/locustfile.py`, and `docs/verification/` |

---

## 1. Purpose and Scope
This document defines the reverse-engineered Test Strategy and Test Plan for `SetuHaul_Rescheduling_Service`, covering unit, integration, property-based, concurrency, trace-replay, security, and load/scale testing, plus verified quality gaps.

## 2. Executive Summary
SetuHaul possesses an unusually thorough automated verification suite: **2,127 pytest tests across 142 test files** (`[Verified]`, `pytest --collect-only -q`), complemented by `ruff` linting, `mypy --strict` static typing, Hypothesis state-machine tests, a 100-run thread/process contention proof (`scripts/run_integrity_proof.py`), a 9-trace deterministic replay evaluator (`scripts/replay_traces.py`), a 23-check end-to-end scenario (`scripts/run_scenario.py`), and a 360-load scale/Locust harness (`scripts/run_scale_profile.py`, `load/locustfile.py`) (`[Verified]`).

The primary testing gaps are: **Linux CI has not been executed since Phase 12** (later phases verified on Windows only), **live-LLM tests have not been re-run since the 2026-11-27 feature wave**, **live prompt-injection tests have never been run**, and **external integrations (Google OAuth, Telegram, SMTP/IMAP, real Redis) are tested only against fakes/stubs** (`[Verified]`).

---

## 3. Multi-Layer Test Strategy `[Verified]`

| Test Layer | Tooling / Harness | Scope & Objective | Verified Evidence |
|---|---|---|---|
| **Static Analysis & Typing** | `ruff==0.17.5`, `mypy==2.4.1` (`strict = true`) | Lint & strict type safety across `src/` and `tests/`. | `.github/workflows/ci.yml:19-20`, `scripts/ci.py:25-34` |
| **Unit & Domain Tests** | `pytest==9.2.0` (`142` `test_*.py` files, `2,127` collected tests) | ETA, GPS, feasibility, holds, confirmation, cancellation, passes, gate, dock blocks, 24/7 slots, facility change, partner mail, RBAC, auth, settings, retention. | `tests/test_*.py` |
| **Property-Based & Invariant Tests** | `hypothesis==6.170.15`, `integrity.check_invariants` (12 SQL invariants + FK check) | Randomized state-machine transitions interleaved with external direct DB inserts; planted-fault tests. | `tests/test_state_properties.py`, `src/setuhaul/integrity.py` |
| **Concurrency & Contention Proof** | `scripts/run_integrity_proof.py`, `src/setuhaul/proof.py` | 20x runs each of 10 threads/1 slot, 6 processes/1 slot, mixed threads, mixed processes, forced 50ms lock timeout. | `docs/verification/concurrency-proof.md:3-127` (100/100 runs, 0 invariant violations) |
| **End-to-End Scenario** | `scripts/run_scenario.py`, `tests/scenario_flow.py` | 6-driver scripted scenario covering rescheduling, hand-offs, confirmation, cross-driver isolation. | `docs/verification/scenario-run.md:87-128` (23/23 checks, 58/58 evaluator checks) |
| **Deterministic Trace Replay & Eval** | `scripts/replay_traces.py`, `src/setuhaul/evaluation.py` | Replays 9 active traces in `tests/traces/` offline through 7 deterministic evaluators + DB invariants. | `docs/running-and-demonstrating.md:173` (9/9 clean, 109/109 checks) |
| **Auth & Security Smoke** | `tests/test_auth_*.py`, `test_security_hardening.py`, `test_injection.py` | RBAC matrix, facility isolation, CSRF same-origin, lockouts, prompt injection (scripted), zero board-value leaks. | `docs/verification/auth-roles-run.md:134-214` |
| **Scale & Load Testing** | `scripts/run_scale_profile.py`, `load/locustfile.py` (`locust==2.47.0`) | 360-load fixture, 35-msg/35s spike, 10-way burst, 200 capacity searches, SQLite writer saturation (1–16 writers). | `docs/verification/scale-test.md:3-53` (stub model, `fakeredis`, Windows) |

---

## 4. Test Plan & Pre-Pilot Entry/Exit Criteria

### 4.1 Automated CI Gate (`scripts/ci.py`, `.github/workflows/ci.yml`) `[Verified]`
1. `python -m ruff check .` -> 0 errors.
2. `python -m mypy src` -> 0 strict type errors.
3. `python -m pytest -v` -> 0 failures (900s timeout in `engineering.yaml:28`).

### 4.2 Required Pre-Pilot Test Execution Plan (Addressing Current Gaps)
1. **Linux Container Verification**: Run full 2,127-test suite inside `python:3.12-slim` Docker container against real `redis:7-alpine`.
2. **Live-LLM Multilingual & Adversarial Suite**: Execute `scripts/run_live_model_scenario.py` and prompt-injection cases (`tests/test_injection.py`) against the approved production LLM in English, Hindi, and Hinglish, measuring median clarification turns ($\le 2$) and auto-resolution rate ($\ge 80\%$).
3. **Real External Integration Smoke**: Verify Google OAuth2 callback, Telegram webhook + `sendMessage`, live SMTP/IMAP partner approval (including SPF/DKIM/DMARC), and Caddy HTTPS proxy headers (`X-Forwarded-For`, `SETUHAUL_PUBLIC_URL`).
4. **Backup & Restore Drill**: Execute `docs/operations/backup-and-restore.md` end-to-end on staging.

---

## 5. Source Evidence, Known Test Gaps & Open Questions
- Known test-harness gap (`TD-05`): `evaluation.WRITE_TOOLS` (`src/setuhaul/evaluation.py:89`) omits `request_facility_change` and `request_partner_slot`, so replay evaluators do not audit those two write tools.
- Open Questions: Which staging environment and approved LLM API key will host the pre-pilot live test run (`Q9`)?
