# DOC-01: Business Case & Business Requirements Specification (BRS)

| Field | Value |
|---|---|
| **Document ID** | `DOC-01` |
| **Version** | `1.0.0` |
| **Category** | 1 — Business & Product |
| **Repository Commit** | `445c7f965e22e7915238cca5631e68b4462d7b7b` |
| **Validation Status** | Verified against repository evidence (missing business financials explicitly marked) |

---

## 1. Purpose and Scope
This document captures the reverse-engineered Business Case and Business Requirements Specification (BRS) for the **SetuHaul Conversational Rescheduling Service** (`setuhaul`). It strictly distinguishes verified operational problem statements and implemented capabilities from missing commercial/financial data that requires stakeholder input.

## 2. Executive Summary
SetuHaul is a working Python/FastAPI/LangGraph/SQLite prototype designed to automate inbound freight dock-appointment rescheduling when truck drivers report delays, breakdowns, or early arrivals in English, Hindi, or Hinglish (`[Verified]`, `README.md:1-10`, `docs/problem-definition.md:3-8`). The system combines a bounded LLM interface for multilingual interpretation with deterministic Python and SQLite services that own identity, ETA derivation, five-gate feasibility, FCFS slot holds, atomic confirmation, cancellations, Dock Access Passes, gate check-in/out, and human escalation (`[Verified]`, `docs/problem-definition.md:19-26`).

**Critical Readiness Caveat**: The service is explicitly classified in repository documentation as **not production-ready** (`[Verified]`, `docs/client-readout.md:8`, `docs/faq.md:20,379`). No real driver has used it, all 6 `BLOCKER` operational policy questions remain unapproved, and commercial business-case metrics (budget, ROI, TCO, named executive sponsors) are **`[Missing]`** from the repository.

---

## 3. Business Case Analysis

### 3.1 Problem Statement `[Verified]`
- Truck drivers message SetuHaul when late, early, or broken down, requiring planners to manually search for feasible dock slots (`docs/problem-definition.md:3-8`).
- During operational spikes (brief target: 20–35 exception messages within 30 minutes across 5 coordinators/shift), concurrent requests compete for scarce dock doors, creating risk of double-booked or infeasible promises (`docs/problem-definition.md:31-32`).

### 3.2 Target Operational Scale vs. Supplied Data Snapshot `[Verified]`
| Dimension | Brief Operational Target (`docs/problem-definition.md:31-32`) | Supplied Seed Snapshot (`data/setuhaul.sqlite3`, `docs/snapshot-limitations.md:3-11`) |
|---|---|---|
| Time span | Daily continuous operations | Single day (`2026-08-04`) |
| Daily loads | 280–360 loads/day | 21 shipments |
| Facilities / Docks | 6 facilities / 32 dock doors | 2 facilities (`FAC-JAI-01`, `FAC-GGN-01`) / 9 docks |
| Daily receiving appointments | 190–240/day | 20 appointments across 106 slots |
| Exception volume | 15–25 normal day; 20–35 per 30-min spike | 10 `driver_exceptions`, 12 `chat_threads` |
| Coordinators | 5 coordinators/shift | Prototype roles (`ADMIN`, `WAREHOUSE_AUTHORITY`, `SECURITY`, Bearer) |
| Labour & consignee data | Required for feasibility | **0 rows** in seed (prevents auto-booking supplied loads without fixtures) |

### 3.3 Commercial & Strategic Elements `[Missing]`
The following standard Business Case artifacts are **not present in the repository** and are **not fabricated**:
- **Original business sponsorship & executive sign-off**: `[Missing]` (see `DOC-23`, Q-BIZ-01).
- **Project budget, LLM operational cost ceiling, and ROI / payback projections**: `[Missing]` (only a technical per-message LLM cost cap of `$0.05` exists in `src/setuhaul/config.py:50,62`).
- **Contractual SLA commitments & carrier penalty schedules**: `[Missing]` (`escalation_sla_minutes = None` in `src/setuhaul/config.py:59`; consignee penalties are boolean flags only).
- **Target rollout geography & go-live timeline**: `[Missing]`.

### 3.4 Explicitly Out-of-Scope Boundaries `[Verified]`
Per `docs/problem-definition.md:76-78`, `docs/decision-answers.md:83-85`, and `docs/faq.md:65-67`, the system explicitly excludes: national network optimization, carrier selection/rates, driver-safety decisions, customs/hazmat workflows, automated penalty approval, facility-wide schedule re-optimization (Phase 13 not built), priority pre-emption of existing holds, voice calls, a native driver web/mobile app, billing, and yard/parking assignment on arrival.

---

## 4. Business Requirements Specification (BRS)

| ID | Business Requirement | Capability | Classification | Source Evidence |
|---|---|---|---|---|
| `BRS-01` | Accept driver delay, breakdown, and early-arrival messages in English, Hindi, and Hinglish without requiring a dedicated driver app. | `BC-01` | Verified | `README.md:3-5`, `src/setuhaul/language.py:29` |
| `BRS-02` | Derive arrival ETAs deterministically from declared times, repair estimates, gate-in evidence, and fresh GPS pings, asking only for missing inputs. | `BC-02` | Verified | `src/setuhaul/eta.py:101-218`, `src/setuhaul/gps.py:71-146` |
| `BRS-03` | Offer only dock slots that pass all physical, operational, labour, equipment, and consignee-commitment constraints; never assume availability when data is missing. | `BC-02` | Verified | `src/setuhaul/feasibility.py:58,152-283` |
| `BRS-04` | Prevent double-booking under concurrent spike load via FCFS temporary holds and atomic swap of old/new appointments. | `BC-03` | Verified | `src/setuhaul/booking.py:214-652`, `migrations/002_appointment_holds.sql:51-66` |
| `BRS-05` | Ensure AI never fabricates booking state, leaks system instructions, or mutates bookings without explicit driver approval. | `BC-01` | Verified | `src/setuhaul/tools.py:1024-1079`, `src/setuhaul/conversation.py:298-318,408-523` |
| `BRS-06` | Hand off unsafe, ambiguous, penalty-bearing, or unresolvable cases to human coordinators with full context and zero silent drops (even during DB outages). | `BC-07` | Verified | `src/setuhaul/escalation.py:68-78,118-135,224-307` |
| `BRS-07` | Support warehouse operations across 24/7 rolling 14-day schedules, dock blocking with displacement planning, facility changes, and partner-warehouse email approvals. | `BC-04`, `BC-05` | Verified | `src/setuhaul/round_clock.py`, `src/setuhaul/dock_blocks.py`, `src/setuhaul/facility_change.py`, `src/setuhaul/partner_booking.py` |
| `BRS-08` | Control physical facility entry and shipment visibility using cryptographic Dock Access Passes verified in chat and at the security gate. | `BC-06` | Verified | `src/setuhaul/dock_pass.py`, `src/setuhaul/passes.py`, `src/setuhaul/gate.py` |
| `BRS-09` | Enforce server-side invite-only authentication, facility-scoped RBAC, encrypted runtime configuration, and immutable audit trails. | `BC-08` | Verified | `src/setuhaul/auth/authz.py`, `src/setuhaul/app_settings.py`, `src/setuhaul/auth/audit.py` |

---

## 5. Source Evidence & Traceability
- Links to Knowledge Model: `BC-01`..`BC-08`, `ACT-01`..`ACT-07`, `RSK-01`, `RSK-02`, `GAP-11`.
- Existing reference docs: `docs/problem-definition.md`, `docs/client-readout.md`, `docs/decision-answers.md`, `docs/snapshot-limitations.md`.

## 6. Assumptions and Limitations
- **Provisional policy defaults**: All 20 operational questions (`Q1`–`Q20` in `docs/operations-questions.md`) use provisional engineering defaults pending operations approval (`[Verified]`).
- **Two unmeasured brief KPIs**: Median clarification turns ($\le 2$) and $\ge 80\%$ auto-resolution on an approved safe set have not been measured on a live LLM (`[Unverified]`, `docs/readiness-detail.md:42-43`).

## 7. Open Questions
- `Q-BIZ-01`: Who are the named business sponsor, product owner, and data protection officer? (`[Missing]`)
- `Q-BIZ-02`: What are the approved budget, target ROI, and monthly LLM spend ceiling? (`[Missing]`)
- `Q-OPS-BLOCKERS`: Approval of `Q3`, `Q4`, `Q6`, `Q9`, `Q17`, `Q20` in `docs/operations-questions.md` (see `DOC-23`).
