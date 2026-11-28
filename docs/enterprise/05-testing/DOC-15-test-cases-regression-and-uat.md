# DOC-15: Functional Test Cases, Integration Test Specification, Regression Catalogue & UAT Specification

| Field | Value |
|---|---|
| **Document ID** | `DOC-15` |
| **Version** | `1.0.0` |
| **Category** | 5 — Quality & Testing |
| **Repository Commit** | `445c7f965e22e7915238cca5631e68b4462d7b7b` |
| **Validation Status** | Reviewed |

---

## 1. Purpose and Scope
This document catalogues the functional test suite mapping, integration test specifications, regression trace baseline, and User Acceptance Testing (UAT) specification for `SetuHaul_Rescheduling_Service`.

## 2. Executive Summary
The repository's 142 pytest modules provide broad, granular coverage of all 15 functional requirement groups (`FR-01`–`FR-15), backed by 9 sanitized regression traces in `tests/traces/`. Because no formal human UAT has been executed with real drivers or warehouse staff, Section 6 defines an actionable UAT specification grounded in verified use cases (`UC-01`–`UC-10).

---

## 3. Functional Test Case Catalogue (Mapped to `FR-01`–`FR-15)

| Suite Area | Representative Test Modules | Verified Behaviors Covered | Traces To |
|---|---|---|---|
| **Conversation, Language & Guardrails** | `test_conversation.py`, `test_language.py`, `test_injection.py`, `test_memory.py`, `test_model_gateway.py`, `test_never_silent.py`, `test_negated_agreement.py`, `test_write_intent.py` | Trilingual intake, unsupported script 2-turn escalation, prompt canary & leak block, claim guard, bounded loop caps, `driver_quote` & prior-turn confirm rules. | `FR-01`, `NFR-03` |
| **ETA, GPS & Feasibility** | `test_eta.py`, `test_gps.py`, `test_gps_store.py`, `test_gps_tools.py`, `test_api_gps.py`, `test_travel.py`, `test_feasibility.py`, `test_option_history.py`, `test_clock_times.py` | Gate-in conflict, late-by baseline, repair+GPS travel, 15m freshness, 30m mismatch, arrival floor, 5-gate evaluation, option reconciliation. | `FR-02`, `FR-03` |
| **Holds, Booking, Cancel & Concurrency** | `test_holds.py`, `test_booking.py`, `test_auto_hold.py`, `test_expiry.py`, `test_cancellation.py`, `test_concurrency.py`, `test_state_properties.py`, `test_edge_cases.py` | FCFS contention, 180s/900s TTL, atomic swap, idempotent confirm/cancel, 120m cancel cutoff, two-turn cancel, Hypothesis invariants. | `FR-04`, `FR-05`, `NFR-01` |
| **Escalations & Warehouse Ops** | `test_escalation.py`, `test_warehouse_cases.py`, `test_operations_actions.py`, `test_board.py`, `test_round_clock.py`, `test_dock_blocks.py`, `test_release.py`, `test_warehouse_screen.py` | Deduplicated cases, JSONL fallback ingest, `ASSIGNED_SLOT` booking, 24/7 14d top-up, dock block preview/`PLAN_CHANGED`/reopen, slot freeing. | `FR-06`, `FR-07` |
| **Facility Change & Partner Email** | `test_facility_change.py`, `test_facility_change_chat.py`, `test_facility_follow_up.py`, `test_move_lifecycle.py`, `test_partner_request.py`, `test_partner_reply.py`, `test_partner_jobs.py`, `test_mail_transport.py` | Destination approval, 15m auto-hold & auto-revert, SMTP/IMAP partner flow, SPF/DKIM/DMARC verification, strict line-1 parser. | `FR-08`, `FR-09` |
| **Shipments, Passes & Gate** | `test_shipment_create.py`, `test_shipment_manage.py`, `test_shipment_visibility.py`, `test_shipment_end_to_end.py`, `test_passes.py`, `test_dock_pass.py`, `test_pass_check.py`, `test_pass_delivery.py`, `test_gate.py`, `test_gate_routes.py`, `test_security_role.py` | `ORD` validation, 128-bit `DAP-...` redaction/delivery, 5-attempt chat lock, `v_driver_shipments` hiding, 8-step gate refusal order, idempotent check-in/out, overstay sweep. | `FR-10`, `FR-11` |
| **Auth, RBAC, Admin & Retention** | `test_auth_google.py`, `test_password_sign_in.py`, `test_auth_local_admin.py`, `test_auth_sessions.py`, `test_auth_authz.py`, `test_auth_role_matrix.py`, `test_security_hardening.py`, `test_admin_api.py`, `test_user_deletion.py`, `test_driver_removal.py`, `test_settings_api.py`, `test_email_settings.py`, `test_telegram_binding.py`, `test_telegram_delivery.py`, `test_retention.py`, `test_refuse_sample_database.py` | Google OIDC+PKCE, email/password links, break-glass lockouts, facility isolation (`404), CSRF same-origin, hybrid user delete, driver soft-delete/restore, Fernet settings, Telegram binding/backoff, retention & erasure. | `FR-12`–`FR-15`, `NFR-04` |

---

## 4. Integration Test Specification
- **HTTP + SQLite + FakeRedis Integration**: `tests/test_api.py`, `test_shipment_routes.py`, `test_exception_routes.py`, `test_gate_routes.py`, `test_telegram_channel.py` exercise FastAPI endpoints end-to-end with real SQLite WAL transactions and `fakeredis`.
- **Container Startup Integration**: `tests/test_deploy_start.py` verifies `deploy/start.py` atomic `.partial` copy, migration application, and idempotent restarts.
- **Unverified Live Integrations (`RSK-03)**: Real Google OAuth, real Telegram Bot API (only 1 historical stub run), real Gmail SMTP/IMAP, real Redis 7 under contention, and Caddy HTTPS reverse proxy.

---

## 5. Regression Catalogue

| Trace File | Type | Purpose | Replay Status |
|---|---|---|---|
| `clarify-options-hold-confirm.json` | Scripted baseline | Clarification -> auto-hold -> confirm happy path | Pass (`replay_traces.py) |
| `hinglish-hold-expiry.json` | Scripted baseline | Hinglish conversation + hold expiry notification | Pass |
| `injection-attempt.json` | Scripted baseline | Prompt-injection attempt blocked by tool/output guards | Pass |
| `never-mind-release.json` | Scripted baseline | Driver releases pending hold without touching original | Pass |
| `no-slot-handoff.json` | Scripted baseline | Zero feasible slots -> complete escalation hand-off | Pass |
| `live-53f1f2f2.json`, `live-ccbe3af8.json`, `live-d9762e12.json` | Live `gpt-6-luna` (2026-11-26) | Single-run English, Hindi, Hinglish one-slot traces (pre-11/27 features) | Pass (17/17 checks each; English turn 1 mis-escalated) |
| `live-f9c78531.json` | Live failure trace | Failed live request regression baseline | Pass |
| `retired/*.json` (3 files) | Retired | Superseded 3-option menu flow (`D65) | Excluded from active replay |

---

## 6. User Acceptance Testing (UAT) Specification
No UAT has been executed with real operational staff. Recommended UAT cases before pilot go-live:
- **`UAT-01` (Driver Trilingual Reschedule)**: Real drivers test English, Hindi, and Hinglish delay/breakdown/early reports via Telegram against the approved live LLM, verifying wording clarity and `Yes`/`No` auto-hold ergonomics.
- **`UAT-02` (Warehouse Daily Operations)**: Warehouse staff configure 24/7 crews, create shipments, approve a facility change, block a dock with displacement preview, and resolve an escalated case with `ASSIGNED_SLOT`.
- **`UAT-03` (Gate Security Flow)**: Gate guards scan a driver's Telegram `DAP-...` pass via camera/wedge, validate, check in, wait for warehouse unloading-complete, and check out.
- **`UAT-04` (Partner Email Approval)**: Real partner warehouse replies `APPROVE`, `DECLINE`, and an ambiguous sentence via Gmail; verify only bare `APPROVE` books and ambiguous escalates.
- **`UAT-05` (Admin Governance & Failover)**: Admin invites/disables/deletes users via Google & Email/Password, tests Settings tab, verifies audit log, and drills DB backup/restore.

## 7. Open Questions
- Open Questions: Who signs off UAT (`UAT-01`–`UAT-05) prior to pilot launch? (`DOC-23)
