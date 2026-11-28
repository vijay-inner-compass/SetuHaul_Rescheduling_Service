# DOC-23: Stakeholder Clarification Questionnaire

| Field | Value |
|---|---|
| **Document ID** | `DOC-23` |
| **Version** | `1.0.0` |
| **Category** | 9 — Traceability, Evidence & Governance |
| **Repository Commit** | `445c7f965e22e7915238cca5631e68b4462d7b7b` |
| **Validation Status** | Consolidates all `[Missing]` & `[Unverified]` items + `docs/operations-questions.md` (`Q1`–`Q20`) |

---

## 1. Purpose and Scope
This questionnaire lists every missing business, operational, security, privacy, and technical decision required from stakeholders before `SetuHaul_Rescheduling_Service` can move from prototype to pilot or production. No answers have been fabricated.

## 2. Executive Summary
There are **6 BLOCKER operational policy questions** (carried over from `docs/operations-questions.md`), **14 proposed operational defaults** awaiting sign-off, and **12 enterprise business/security/privacy/architecture clarifications** discovered during reverse-engineering (`[Missing]`).

---

## 3. Priority 1 — Pilot Blockers (Must Answer Before Pilot)

| Q-ID | Existing Ref | Owner Role | Question | Current Code Default |
|---|---|---|---|---|
| `Q-OPS-01` | `Q3` (BLOCKER) | Warehouse Ops Lead | What ETA buffer (minutes) is required before dock release? | `0 min` (except `15 min` on GPS/facility-change search, `TD-06`) |
| `Q-OPS-02` | `Q4` (BLOCKER) | Ops Manager | Are priority classes (`CRITICAL/HIGH/NORMAL/LOW`) advisory only, or can high priority pre-empt an existing hold? | Advisory only; FCFS at hold commit (`BR-06`) |
| `Q-OPS-03` | `Q6` (BLOCKER) | Ops Manager | What is the approved self-service cancellation cutoff before slot start? | `120 min` + known penalty-free consignee commitment (`BR-09`) |
| `Q-OPS-04` | `Q9` (BLOCKER) | Eng Lead + Legal/DPO | Which LLM provider, model ID, data-processing agreement (DPA), and monthly budget are approved for real driver traffic? | None approved (`gpt-6-luna` used for synthetic test only) |
| `Q-OPS-05` | `Q17` (BLOCKER) | Warehouse Ops Lead | What is the authoritative source of `labour_windows` and `consignee_commitments`? (Seed DB has 0 rows; 24/7 `facility_crews` covers labour only.) | Fail-closed `INCOMPLETE_FEASIBILITY_DATA` (`BR-04`) |
| `Q-OPS-06` | `Q20` (BLOCKER) | Warehouse Ops Lead | How should loads whose `expected_unload_min` exceeds the 60-min slot (e.g., 75-min `CRITICAL` loads) be handled? | Strict fit -> escalated to human (`BR-05`) |

---

## 4. Priority 2 — Business Case, Governance & Legal (`[Missing]`)

| Q-ID | Owner Role | Question |
|---|---|---|
| `Q-BIZ-01` | Executive Sponsor | Who are the named business sponsor, product owner, operations lead, and Data Protection Officer (DPO)? |
| `Q-BIZ-02` | Finance / Product | What are the approved project budget, target ROI / cost-per-reschedule KPI, and pilot success criteria? |
| `Q-BIZ-03` | Product / Ops | What are the formal availability SLA, RTO/RPO targets, and escalation response SLA (`Q16`, currently `None`)? |
| `Q-PRV-01` | Legal / DPO | Which privacy statutes apply (India DPDP Act 2023, GDPR, etc.), what retention periods are legally required per table (`chat_messages`, `gps_pings`, `audit_log`), and what privacy/consent wording must the Telegram bot show before binding? |
| `Q-PRV-02` | Legal / Security | Is database encryption at rest (disk/volume encryption or SQLCipher/PostgreSQL TDE) mandatory for pilot? |
| `Q-UAT-01` | Ops Manager | Who will review and approve native Hindi and Hinglish driver templates (`RSK-06`) and sign off `UAT-01`–`UAT-05` (`DOC-15`)? |

---

## 5. Priority 3 — Architecture, Integrations & Security Clarifications

| Q-ID | Existing Ref | Owner Role | Question |
|---|---|---|---|
| `Q-INT-01` | `Q8` | Ops / Enterprise Architect | Which external channel (email, webhook, ERP/WMS) should consume `WAREHOUSE_PLANNER`, `CONSIGNEE`, and `OPERATIONS` rows in `notification_outbox` (`TD-03`)? |
| `Q-INT-02` | — | Security Architect | Should Google OAuth verify ID token JWKS signatures (instead of relying solely on direct TLS token endpoint, `D73`), and should Coordinator Bearer tokens be scoped per facility and rotatable without restart? |
| `Q-INT-03` | — | Engineering Lead | Can deprecated `GET /v1/driver/status` (which puts driver phone in query string) be retired immediately? |
| `Q-OPS-07` | `Q1, Q2, Q18, Q19` | Warehouse Ops Lead | Confirm provisional timers: hold TTL `180s` (proactive `900s`), sweep `30s`, facility freshness `5m`, option lifetime `15m`. |
| `Q-OPS-08` | `Q10, Q11, Q12–Q15, D15, D42, D78, D88` | Product / Ops / Eng | Confirm remaining proposed defaults: `en/hi/hinglish` language scope, `20/m & 100/h` rate limits, `24h` case reopen window, `30d` option retention, `$0.05` LLM cap, sticky Telegram revoke, and `ACTIVE`/`OFF_DUTY` driver eligibility. |
| `Q-OPS-09` | `GAP-08` | DevOps / Security | Confirm that Hostinger backup scripts will store `SETUHAUL_SETTINGS_KEY`/`.env` separately from SQLite archives per `docs/operations/backup-and-restore.md`. |

## 6. Source Evidence
- `docs/operations-questions.md:5-26`, `docs/decision-register.md`, `docs/faq.md`, `docs/client-readout.md`.
