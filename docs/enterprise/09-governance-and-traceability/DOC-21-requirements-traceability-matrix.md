# DOC-21: Bidirectional Requirements Traceability Matrix (RTM)

| Field | Value |
|---|---|
| **Document ID** | `DOC-21` |
| **Version** | `1.0.0` |
| **Category** | 9 — Traceability, Evidence & Governance |
| **Repository Commit** | `445c7f965e22e7915238cca5631e68b4462d7b7b` |
| **Validation Status** | Reviewed |

---

## 1. Purpose and Scope
This Requirements Traceability Matrix (RTM) establishes full bidirectional traceability from Business Requirements (`BRS-*) and Capabilities (`BC-*) through Functional Requirements (`FR-*), Use Cases (`UC-*), User Stories (`US-*), Business Rules (`BR-*), Architectural Components (`CMP-*), Source Code / Migrations, and Automated Test Evidence.

## 2. Executive Summary
All **15 Functional Requirements (`FR-01`–`FR-15)** trace forward to verified implementation modules and automated pytest suites. Traceability analysis surfaced **0 unimplemented functional requirements** in code, **1 undocumented recent feature** (`ADR-12` / `GAP-04` web user soft-deletion), **1 partially unimplemented downstream outbox transport** (`TD-03), and **2 unverified brief KPIs** (live-LLM median turns & auto-resolution rate).

---

## 3. Master Bidirectional Traceability Matrix

| BRS | Capability | FR | UC / US | Rules | Components | Primary Source & Migrations | Primary Automated Tests | Status |
|---|---|---|---|---|---|---|---|---|
| `BRS-01`, `BRS-05` | `BC-01` | `FR-01` | `UC-01`, `US-01` | `BR-01`, `BR-10` | `CMP-02` | `conversation.py`, `language.py`, `memory.py`, `prompts.py`, `model_gateway.py`; `005`, `008` | `test_conversation.py`, `test_language.py`, `test_injection.py`, `test_memory.py`, `test_model_gateway.py` | Verified (scripted); Live LLM |
| `BRS-02` | `BC-02` | `FR-02` | `UC-01`, `UC-02` | `BR-01`, `BR-02`, `BR-03` | `CMP-03` | `eta.py`, `gps.py`, `travel.py`, `position.py`; `009` | `test_eta.py`, `test_gps.py`, `test_travel.py`, `test_api_gps.py` | Verified |
| `BRS-03` | `BC-02` | `FR-03` | `UC-01` | `BR-04`, `BR-05` | `CMP-03` | `capacity.py`, `feasibility.py`, `allocation.py`, `option_history.py`; `001`, `006`, `026` | `test_feasibility.py`, `test_option_history.py` | Verified (seed lacks labour/consignee) |
| `BRS-04` | `BC-03` | `FR-04` | `UC-01`, `US-01`, `US-02`, `US-04` | `BR-06`, `BR-07`, `BR-08` | `CMP-04`, `CMP-09`, `CMP-10` | `booking.py`, `jobs/expire_holds.py`; `002`, `003`, `007` | `test_holds.py`, `test_booking.py`, `test_auto_hold.py`, `test_expiry.py`, `test_concurrency.py`, `test_state_properties.py` | Verified (100 contention runs) |
| `BRS-01`, `BRS-05` | `BC-01` | `FR-05` | `UC-03`, `US-03` | `BR-09`, `BR-10` | `CMP-02`, `CMP-04` | `cancellation.py`, `tools.py`; `007` | `test_cancellation.py`, `test_write_intent.py` | Verified |
| `BRS-06` | `BC-07` | `FR-06` | `UC-09` | `BR-04`, `BR-09` | `CMP-07`, `CMP-01` | `escalation.py`, `coordinator.py`, `assign.py`, `api.py`; `004` | `test_escalation.py`, `test_warehouse_cases.py`, `test_operations_actions.py` | Verified |
| `BRS-07` | `BC-04` | `FR-07` | `UC-08`, `US-05`, `US-06` | `BR-11` | `CMP-04`, `CMP-09` | `board.py`, `round_clock.py`, `dock_blocks.py`, `release.py`; `018`, `022` | `test_board.py`, `test_round_clock.py`, `test_dock_blocks.py`, `test_release.py` | Verified (`TD-08` minor leaks) |
| `BRS-07` | `BC-05` | `FR-08` | `UC-04`, `US-07` | `BR-12` | `CMP-05`, `CMP-01` | `facility_change.py`, `auth/facility_routes.py`; `013` | `test_facility_change.py`, `test_facility_change_chat.py`, `test_facility_follow_up.py` | Verified (`TD-06` buffer) |
| `BRS-07` | `BC-05` | `FR-09` | `UC-05`, `US-08` | `BR-13` | `CMP-05`, `CMP-09` | `partner_booking.py`, `mail_transport.py`, `mailcfg.py`, `jobs/partner_mail.py`; `010` | `test_partner_request.py`, `test_partner_reply.py`, `test_partner_jobs.py`, `test_mail_transport.py` | Verified (simulated); Live mail |
| `BRS-08` | `BC-06` | `FR-10` | `UC-06`, `US-09` | `BR-14` | `CMP-06`, `CMP-01` | `auth/shipments.py`, `dock_pass.py`, `passes.py`, `pass_check.py`, `order_numbers.py`; `016` | `test_shipment_create.py`, `test_passes.py`, `test_pass_check.py`, `test_pass_delivery.py`, `test_shipment_end_to_end.py` | Verified |
| `BRS-08` | `BC-06` | `FR-11` | `UC-07`, `US-10` | `BR-14` | `CMP-06`, `CMP-01` | `gate.py`, `auth/gate_routes.py`, `auth/gate_pages.py`; `016`, `017` | `test_gate.py`, `test_gate_routes.py`, `test_security_role.py` | Verified (`TD-07` CSP style) |
| `BRS-09` | `BC-08` | `FR-12` | `UC-10`, `US-11` | `BR-15` | `CMP-08`, `CMP-01` | `identity.py`, `rate_limit.py`, `auth/routes.py`, `password_routes.py`, `google.py`, `local_admin.py`, `sessions.py`, `authz.py`, `telegram.py`; `005`, `011`, `017`, `020`, `024` | `test_auth_google.py`, `test_password_sign_in.py`, `test_auth_local_admin.py`, `test_auth_role_matrix.py`, `test_telegram_binding.py`, `test_security_hardening.py` | Verified (fakes); Live Google/TG |
| `BRS-09` | `BC-08` | `FR-13` | `UC-10`, `US-12` | `BR-15`, `BR-16` | `CMP-08`, `CMP-11` | `auth/admin_api.py`, `settings_routes.py`, `app_settings.py`, `model_report.py`, `driver_removal.py`, `auth/users.py`; `011`, `014`, `015`, `019`, `027` | `test_admin_api.py`, `test_user_deletion.py`, `test_driver_removal.py`, `test_settings_api.py`, `test_model_tab.py` | Verified (`GAP-04` missing ADR) |
| `BRS-07` | `BC-04` | `FR-14` | `UC-09` | `BR-15` | `CMP-07`, `CMP-09` | `driver_messages.py`, `chat_loads.py`, `jobs/send_driver_notifications.py`; `021`, `023` | `test_tell_driver.py`, `test_chat_loads.py`, `test_telegram_delivery.py`, `test_send_backoff.py` | Verified |
| `BRS-09` | `BC-08` | `FR-15` | `UC-10` | `BR-15` | `CMP-08`, `CMP-10` | `retention.py`; `025` | `test_retention.py` | Verified (OFF by default; no UI/export) |

---

## 4. Traceability Findings (Phase 6 Validation)
- **Undocumented Functionality**: Hybrid web user soft-deletion (`migrations/027_user_deletions.sql`, `auth/users.py) is implemented and tested (`test_user_deletion.py) but absent from `docs/decision-register.md` (`GAP-04).
- **Unimplemented Documented Requirements**: Outbound notification transport for `WAREHOUSE_PLANNER`/`CONSIGNEE`/`OPERATIONS` (`TD-03); formal DPDP/GDPR consent & DSAR export (`RSK-04).
- **Untested / Partially Tested Requirements**: Live-LLM behavior post-2026-11-27 (`RSK-01), live prompt injection, real Google OAuth / SMTP / IMAP / Redis / Hostinger deploy (`RSK-03), `request_facility_change` & `request_partner_slot` in `evaluation.WRITE_TOOLS` (`TD-05).
- **Conflicting Rules / Inconsistencies**: `passes.on_site` vs `release.truck_inside` (`TD-02); `deployment-hostinger.md` vs `backup-and-restore.md` key separation (`GAP-08).
