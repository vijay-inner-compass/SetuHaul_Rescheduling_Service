# DOC-13: Privacy Specification, Risk Register, SBOM & Governance Assessment

| Field | Value |
|---|---|
| **Document ID** | `DOC-13` |
| **Version** | `1.0.0` |
| **Category** | 4 — Security, Privacy & Governance |
| **Repository Commit** | `445c7f965e22e7915238cca5631e68b4462d7b7b` |
| **Validation Status** | Reviewed |

---

## 1. Purpose and Scope
This document provides the Personal Data & Privacy Specification, Enterprise Risk Register (`RSK-01`–`RSK-07), Software Bill of Materials (SBOM), and AI/Data Governance Assessment for `SetuHaul_Rescheduling_Service`.

## 2. Executive Summary
SetuHaul processes sensitive personal and operational data (driver names, E.164 phone numbers, driving licence numbers, GPS coordinates, chat transcripts, staff emails, client IPs). While engineering controls exist for scrubbing logs/LangSmith traces, opt-in retention (`retention.py), and FK-preserving driver PII erasure (`erase_driver_data), **regulatory compliance (India DPDP Act / GDPR) has NOT been legally assessed**, retention is **OFF by default**, there is **no database encryption at rest**, and there is **no privacy notice or consent flow**.

---

## 3. Privacy & Personal Data Specification

### 3.1 Personal Data Inventory
| Data Subject | PII Fields | Storage Tables | External Transmission | Retention / Erasure |
|---|---|---|---|---|
| **Truck Driver (`ACT-01)** | `driver_name`, `phone`, `licence_number`, `home_base_city`, `telegram_user_id`, `chat_id`, GPS `lat`/`lon`, chat transcripts | `drivers`, `channel_identities`, `telegram_identities`, `driver_profiles`, `gps_pings`, `chat_messages`, `inbound_messages` | Telegram Bot API; LLM provider (chat text, no `driver_id); optional LangSmith (scrubbed phones/emails; `HIDE_TEXT=1` recommended). Partner emails explicitly omit driver PII. | Opt-in (`SETUHAUL_RETENTION_ENABLED=1): `chat_messages` 90d, `gps_pings` 30d, `inbound_messages` 30d. `erase_driver_data` scrubs `drivers` to `ERASED` & deletes chats/GPS/identities (no HTTP/UI endpoint yet). |
| **Staff Users (`ACT-02..04)** | `email`, `display_name`, password hash (`scrypt), session hash, client `ip`, `original_email` | `users`, `auth_sessions`, `admin_credentials`, `audit_log`, `auth_ip_lockouts`, `user_deletions` | Google OIDC; SMTP server (invite/reset links). | Opt-in: `audit_log.ip` scrubbed to `NULL` after 90d (`025). Hybrid delete rewrites email to `deleted+<id>@deleted.invalid` if referenced. |
| **Partner / Facility Contacts (`ACT-07)** | `contact_name`, `email`, `phone` | `facility_contacts`, `partner_inbound` | SMTP/IMAP mail server. | Permanently retained ( policy). |

### 3.2 Privacy Compliance Gaps
- **No DPDP / GDPR Assessment**: Compliance status is /.
- **No Consent / Privacy Notice**: Telegram bot onboarding contains no privacy policy or consent capture.
- **No Self-Service DSAR Export**: `erase_driver_data` exists only as a Python function; no export or UI workflow exists.
- **No DB Encryption at Rest**: SQLite file `/app/state/setuhaul.db` is unencrypted on disk (only 5 `app_settings` secrets are Fernet-encrypted).

---

## 4. Enterprise Risk Register (`RSK-01`–`RSK-07)

| Risk ID | Severity | Risk Description | Recommended Mitigation |
|---|---|---|---|
| `RSK-01` | **Critical** | Live LLM behavior unvalidated after 2026-11-27 changes (Dock Access Pass, two-turn cancel, strict Yes); zero live prompt-injection tests; brief acceptance KPIs unmeasured. | Run full trilingual live-LLM & adversarial prompt-injection evaluation suite before any pilot. |
| `RSK-02` | **Critical** | All 6 `BLOCKER` operational policy questions (`Q3, Q4, Q6, Q9, Q17, Q20) unapproved; supplied DB has 0 labour/consignee rows. | Obtain written operations sign-off and connect live labour/consignee data feeds (`DOC-23). |
| `RSK-03` | **High** | External integrations (Google OAuth, SMTP/IMAP, real Redis, Hostinger/ForgeCI, Caddy TLS) never verified end-to-end in production. | Execute staging integration verification across all external dependencies. |
| `RSK-04` | **High** | DPDP/GDPR compliance unassessed; no DB encryption at rest; retention disabled by default; no privacy notice or DSAR export. | Complete DPIA/legal review, enable disk encryption & retention, add bot privacy notice. |
| `RSK-05` | **High** | Single-node SQLite SPOF; write saturation at ~244 holds/s (p99 4.2s at 16 writers); backup/restore never drilled. | Execute backup/restore drill; plan PostgreSQL + worker queue migration (`DOC-20). |
| `RSK-06` | **Medium** | Hindi/Hinglish templates AI-authored and unreviewed by native speakers; regex-based language & claim guards. | Commission native-speaker review and expand adversarial multilingual test traces. |
| `RSK-07` | **Medium** | Security hardening gaps: CSP inline style (`/gate), `/auth/local-admin` missing Content-Type/body cap, missing audit IPs, fail-open driver rate limit, no rate limit on OAuth/Telegram/GPS, no HSTS/API security headers. | Remediate all hardening items in Phase 1 of Modernization Roadmap (`DOC-20). |

---

## 5. Software Bill of Materials (SBOM)

### 5.1 Base Container Images & Runtime
- **Python**: `>=3.12,<3.13`, bundled SQLite.
- **App Base Image**: `python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b`.
- **Cache Image**: `redis:7-alpine@sha256:7f7d30b7fdd09467bac58198a1f166dd69789d348d4fe859cccb20e0cf287fbe`.
- **Frontend**: Zero third-party JS/CSS packages.

### 5.2 Direct Python Dependencies
| Package | Pinned Version | Scope | Purpose / Notes |
|---|---|---|---|
| `fastapi` | `0.164.0` | Runtime | HTTP/ASGI web framework |
| `uvicorn` | `0.54.0` | Runtime | ASGI server |
| `pydantic` | `2.14.3` | Runtime | Strict schema validation (`extra="forbid") |
| `httpx` | `0.28.1` | Runtime | Outbound HTTPS (LLM, Telegram, Google OAuth) |
| `tzdata` | `2026.6` | Runtime | IANA timezone DB (`Asia/Kolkata` on Windows) |
| `redis` | `8.1.0` | Runtime | Redis client for cache, rate limits, OAuth state |
| `langgraph` | `1.2.23` | Runtime | Bounded conversational state-graph loop |
| `cryptography` | `50.0.2` | Runtime | Fernet encryption for runtime settings |
| `opentelemetry-api` | `1.45.2` | Runtime | **Unused in `src/setuhaul/` (`TD-04)** |
| `opentelemetry-sdk` | `1.45.2` | Runtime | **Unused in `src/setuhaul/` (`TD-04)** |
| `pytest` | `9.2.0` | Dev | Automated test runner |
| `hypothesis` | `6.170.15` | Dev | Property-based & state-machine testing |
| `fakeredis` | `2.40.0` | Dev | In-process Redis fake for tests/demos |
| `ruff` | `0.17.5` | Dev | Linter |
| `mypy` | `2.4.1` | Dev | Static type checker (`strict = true) |
| `locust` | `2.47.0` | Dev | Load testing |
| `pre-commit` | `4.7.0` | Dev | Git pre-commit hook runner |
| `setuptools` | `80.9.0` | Build | PEP 517 build backend |

- **Transitive Lockfile**: `docs/requirements-lock.txt` pins **100** resolved packages (including `starlette==1.10.0`, `anyio==4.16.0`, `langchain-core==1.8.3`, `langsmith==0.15.0`, `orjson==3.13.0`, `urllib3==2.8.0`, `certifi==2026.10.23).
- **Vulnerability Scanning Status**: — no automated SCA/CVE scan (e.g., `pip-audit`, Trivy, Dependabot) is configured in `.github/workflows/ci.yml` or `scripts/ci.py`.

---

## 6. AI & Platform Governance Assessment
- **Model Governance**: Strong deterministic guardrails (bounded loop, cost cap `$0.05`, `model_turns`/`model_calls` telemetry, triple-opt-in scrubbed LangSmith, Admin Configuration tab).
- **Governance Gaps**: No approved production LLM vendor/DPA (`Q9), no automated CVE/SBOM CI check, no external security audit, and no formal change-management/release-signoff workflow.

## 7. Open Questions
- Open Questions: Legal jurisdiction (India DPDP vs GDPR), required retention durations, and approved LLM data-processing agreement (`DOC-23).
