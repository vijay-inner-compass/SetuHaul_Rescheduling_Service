# DOC-20: Technical Debt Register, Maintainability Assessment & Modernization Roadmap

| Field | Value |
|---|---|
| **Document ID** | `DOC-20` |
| **Version** | `1.0.0` |
| **Category** | 8 — Technical Debt & Modernization |
| **Repository Commit** | `445c7f965e22e7915238cca5631e68b4462d7b7b` |
| **Validation Status** | Verified against codebase and verification reports |

---

## 1. Purpose and Scope
This document catalogues all verified Technical Debt items (`TD-01`–`TD-12`), assesses overall codebase maintainability, and defines a phased Remediation & Modernization Roadmap to take `SetuHaul_Rescheduling_Service` from prototype to production-grade enterprise readiness.

## 2. Executive Summary
The codebase demonstrates **high structural maintainability** for a prototype: strict `mypy` typing, clean modular boundaries, injectable `Clock`, `2,127` automated tests, and zero frontend framework sprawl (`[Verified]`). However, **12 concrete technical debt items** (`3 High`, `7 Medium`, `2 Low`) and the single-process SQLite scaling ceiling must be remediated systematically before production rollout (`[Verified]`).

---

## 3. Technical Debt Register (`TD-01`–`TD-12`) `[Verified]`

| ID | Severity | Area | Description & Impact | Exact Source Evidence | Recommended Remediation |
|---|---|---|---|---|---|
| `TD-01` | **High** | Scalability / HA | Single-process SQLite + in-process daemon threads (`MaintenanceRunner`, `DriverNoticeRunner`, `PartnerMailRunner`) + in-memory locks/backoff cap deployment at 1 worker (~244 holds/s, p99 4.2s at 16 writers). | `docs/verification/scale-test.md:45-52`, `jobs/send_driver_notifications.py:121-134`, `passes.py:257-267`, `api.py:554-562` | Migrate to PostgreSQL (`SELECT FOR UPDATE` / advisory locks) + external worker process (e.g., ARQ/Celery/Redis Streams) when scaling triggers are met. |
| `TD-02` | **High** | Domain Integrity | Divergent "truck on site" predicates (`passes.on_site` checks `truck_visits` only vs `release.truck_inside` checks both `shipments.current_status` and `truck_visits`), and `partner_booking._book()` omits `passes.on_site()`. | `passes.py:66-69`, `release.py:68-73`, `partner_booking.py:405-413` | Unify on a single canonical `is_truck_on_site(conn, shipment_id)` predicate and invoke it inside `partner_booking._book()`. |
| `TD-03` | **High** | Integrations | `notification_outbox` rows for `WAREHOUSE_PLANNER`, `CONSIGNEE`, and `OPERATIONS` have no production sender and accumulate `PENDING` indefinitely. | `jobs/deliver_notifications.py:3-6`, `docs/deployment-hostinger.md:186` | Build recipient-filtered outbound delivery workers (email/webhook) or disable unused fan-out until target channels are approved (`Q8`). |
| `TD-04` | **Medium** | Observability | `opentelemetry-api` and `opentelemetry-sdk` are pinned in `pyproject.toml` but never imported; `observability.py` only has `InMemoryExporter`; no metrics/alerting. | `pyproject.toml:18-19`, `observability.py:59-127` | Either wire real OTel OTLP/Prometheus exporters (latency, lock waits, model errors, fallback file size) or remove unused deps. |
| `TD-05` | **Medium** | AI Guardrails | `tools.py:1076` omits `"REQUESTED"` from `writes_done`; `search_slot_options` (`write=False`) can auto-hold after a write in the same turn; `evaluation.WRITE_TOOLS` omits `request_facility_change` & `request_partner_slot`. | `tools.py:936-952,1043-1077`, `evaluation.py:89` | Include `"REQUESTED"` in `writes_done`, enforce `writes_done == 0` before `_auto_hold`, and add both tools to `evaluation.WRITE_TOOLS`. |
| `TD-06` | **Medium** | Scheduling | `position.py:149` and `facility_change.py:291` persist `eta_buffer_minutes=15` into `option_sets.context_json`, causing `booking.confirm_reschedule()` to re-apply a 15m buffer at confirmation time. | `position.py:149`, `facility_change.py:291`, `booking.py:571` | Separate hold-TTL search buffer from confirmation arrival check (`eta_buffer_minutes=0` or configured `Q3` buffer). |
| `TD-07` | **Medium** | Security | CSP inline-style violation in `gate_pages.py:25` (`style="max-width:320px;width:100%"`); `POST /auth/local-admin` lacks `Content-Type: application/json` check and `read_body_capped`. | `auth/gate_pages.py:25`, `auth/routes.py:247-276` | Move video styles to `app.css`; add `application/json` & body-cap guards to `/auth/local-admin`. |
| `TD-08` | **Medium** | Scheduling / UI | Expired holds leak into `board.py` owner dict and hide slots in `assign.open_slots_page()`; `cancellation.request_original_slot_again()` risks `KeyError` if prior slot is older than `WINDOW_BACK` (2d). | `board.py:23-25`, `assign.py:90`, `cancellation.py:281` | Filter `expires_epoch > now` in `board.py` & `assign.py`; use `.get()` with graceful fallback in `request_original_slot_again()`. |
| `TD-09` | **Medium** | Background Jobs | `PartnerMailRunner.run_once` calls `record_runner_success` even if inner steps raised exceptions; `MaintenanceRunner` starts unconditionally when `deps.settings` is set, ignoring `SETUHAUL_RUN_MAINTENANCE=0`. | `jobs/partner_mail.py:81-93`, `api.py:446,1256` | Track `_pass_failed` in `PartnerMailRunner` (matching `MaintenanceRunner`); honor explicit `SETUHAUL_RUN_MAINTENANCE=0`. |
| `TD-10` | **Medium** | Performance | O(N) full-table Python scans in `retention.py`, `telegram.py`, `onboarding.py`, `shipments.py`; string ISO timestamp comparisons in `round_clock.py`, `partner_booking.py`, `dock_blocks.py`. | `retention.py:62-135`, `auth/telegram.py:117-118`, `round_clock.py:47`, `partner_booking.py:104-105` | Add normalized index columns (e.g., E.164 phone, normalized order ref, epoch timestamps) and migrate SQL comparisons to epoch/indexed queries. |
| `TD-11` | **Low** | Security / UX | Most authenticated mutations pass `ip=None` to `audit.record()`; `sh_facility` cookie lacks `Secure` flag; Admin top nav omits `/gate`; deprecated `GET /v1/driver/status` still live; JSON APIs lack `Cache-Control: no-store`. | `auth/admin_api.py`, `auth/pages.py:559,1024,1117`, `api.py:912-925` | Pass `client_ip(request)` to all audit calls, set `Secure` on `sh_facility`, add `/gate` to Admin nav, add JSON security headers, retire `GET /v1/driver/status`. |
| `TD-12` | **Low** | Resource Mgmt | Unbounded in-memory dicts (`driver_locks`, `_PASS_LOCKS`, `langsmith_trace._cache`, `langsmith_report._cache`); `SettingsSource._bot` swaps `RealTelegramClient` without locking or closing previous `httpx.Client`. | `api.py:554-562`, `passes.py:257-267`, `langsmith_trace.py:180`, `runtime_settings.py:46,86-89` | Use LRU/TTL caches, lock bot swap & close old `httpx.Client`, and clean up unused per-driver locks. |

---

## 4. Maintainability Assessment `[Verified]`

| Dimension | Rating | Evidence |
|---|---|---|
| **Code Organization & Modularity** | **Strong** | Clean separation across ~108 focused Python modules; zero circular spaghetti; zero frontend bundler debt. |
| **Type Safety & Linting** | **Strong** | `mypy --strict` across `src/` + `ruff` (`line-length=100`, `py312`). |
| **Testability** | **Strong** | Injectable `Clock`, `ModelGateway` protocol, `InMemoryMailbox`, `fakeredis`, 2,127 tests, Hypothesis property tests, 12 DB invariants. |
| **Documentation Freshness** | **Moderate (drifting)** | Rich existing docs, but 10+ verified drift items (`GAP-01`–`GAP-10`, e.g., migration count 23 vs 27, test count 1,972 vs 2,127, backup key contradiction). |
| **Operational / Production Readiness** | **Low (Prototype)** | Never deployed to Linux/VPS, no monitoring/alerting, no SCA/CVE scanning, no live-LLM validation post-11/27. |

---

## 5. Phased Modernization & Remediation Roadmap

### Phase 1: Pre-Pilot Hardening & Bug Fixes (Weeks 1–2)
1. Fix `TD-02` (unify on-site check), `TD-05` (tool write accounting & evaluator coverage), `TD-06` (15m buffer at confirm), `TD-07` (CSP & `/auth/local-admin`), `TD-08` (board/assign expired holds & `KeyError`), `TD-09` (`PartnerMailRunner` health & `RUN_MAINTENANCE` flag), `TD-11` (audit IPs, `sh_facility` Secure, JSON headers, Admin `/gate` link).
2. Reconcile existing docs (`GAP-01`–`GAP-10`), especially `GAP-08` backup key separation.
3. Obtain stakeholder sign-off on all 6 `BLOCKER` operational questions (`Q3, Q4, Q6, Q9, Q17, Q20`) and complete DPDP/GDPR privacy assessment + bot consent text (`RSK-02`, `RSK-04`).

### Phase 2: Staging Verification & Pilot Launch (Weeks 3–4)
1. Enable Linux CI (`.github/workflows/ci.yml`) + SCA (`pip-audit`) + native-speaker Hindi/Hinglish review (`RSK-06`).
2. Run live-LLM trilingual & adversarial prompt-injection evaluation suite (`RSK-01`) and measure brief KPIs ($\le 2$ median turns, $\ge 80\%$ auto-resolution).
3. Deploy to staging VPS behind Caddy with real Redis 7, Google OAuth, Telegram bot, and Gmail SMTP/IMAP; execute backup/restore drill (`RSK-03`, `RSK-05`).
4. Wire OTel/Prometheus metrics & alerting (`TD-04`) and enable retention (`SETUHAUL_RETENTION_ENABLED=1`) + disk encryption.

### Phase 3: Enterprise Scale & Multi-Worker Modernization (Months 2–4)
1. Migrate SQLite -> PostgreSQL (`TD-01`, triggered by >1 writer service, >4 workers, or >1% lock timeouts) with Alembic migrations and connection pooling.
2. Externalize `MaintenanceRunner`, `DriverNoticeRunner`, and `PartnerMailRunner` into dedicated worker processes with Redis/Postgres job queues so API servers scale horizontally.
3. Implement downstream planner/consignee/ERP notification connectors (`TD-03`), facility-scoped rotatable service tokens, and indexed queries (`TD-10`).

## 6. Source Evidence & Open Questions
- Evidence: `src/setuhaul/`, `docs/verification/scale-test.md`, `docs/faq.md`, `docs/readiness-detail.md`.
- Open Questions: Confirm pilot facility count and expected peak concurrency to schedule Phase 3 PostgreSQL migration (`DOC-23`).
