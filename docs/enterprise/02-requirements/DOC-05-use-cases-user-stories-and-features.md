# DOC-05: Use Case Specifications, User Stories, Acceptance Criteria & Feature Catalogue

| Field | Value |
|---|---|
| **Document ID** | `DOC-05` |
| **Version** | `1.0.0` |
| **Category** | 2 — Requirements & UX |
| **Repository Commit** | `445c7f965e22e7915238cca5631e68b4462d7b7b` |
| **Validation Status** | Reviewed |

---

## 1. Purpose and Scope
This document provides structured Use Case Specifications (`UC-01`–`UC-10), Agile User Stories with Given/When/Then Acceptance Criteria (`US-01`–`US-12), and the complete Feature Catalogue derived from the implemented system.

## 2. Executive Summary
All 10 primary use cases and 12 user stories correspond directly to implemented code paths and automated tests across driver rescheduling, GPS reassessment, cancellations, facility changes, partner warehouse email approvals, warehouse dock operations, Dock Access Pass gate control, and platform administration.

---

## 3. Use Case Specifications

| UC ID | Use Case | Primary Actor | Trigger | Preconditions | Main Success Flow | Key Exception / Failure Paths | Traces To |
|---|---|---|---|---|---|---|---|
| `UC-01` | Report Delay & Auto-Hold Reschedule | `ACT-01` Driver | Driver sends delay/breakdown message via Telegram | Driver eligible & Telegram-bound; shipment confirmed if app-created | 1. Language detected (`en/hi/hinglish). 2. LLM calls `search_slot_options`. 3. Deterministic ETA + GPS floor + 5-gate search. 4. Rank-1 slot auto-held (`180s). 5. Driver replies `Yes`. 6. Atomic swap ends old, confirms new, syncs pass. | Missing ETA info -> single question; `NO_SLOT`/`INCOMPLETE_DATA`/`APPROVAL_REQUIRED` -> escalation; `No` -> decline & auto-hold next; timeout -> hold expires. | `FR-01`–`FR-04` |
| `UC-02` | Proactive GPS Position Reassessment | `ACT-06` / `ACT-01` | Telematics ping or driver location shared | Fresh ping ($\le 15\text{m}$), non-future ($\le +300\text{s}$) | 1. Store ping. 2. Reassess live holds; release if unreachable (`POSITION_CHANGED). 3. If slot $\ge 30\text{m}$ earlier exists and no live hold, auto-hold for `900s` and notify driver. | Stale ping ignored; no earlier slot -> announce position received only. | `FR-02`, `FR-04` |
| `UC-03` | Two-Turn Appointment Cancellation | `ACT-01` Driver | Driver asks to cancel appointment | Driver has active `CONFIRMED` appointment | 1. Turn 1 previews cancel & stores `PendingCancel` (`10m). 2. Turn 2 driver replies `Yes`. 3. System cancels appointment idempotently and renders confirmation. | Truck on-site/`IN_PROGRESS` -> refused; $<120\text{m}$ or penalty/unknown commitment -> escalated; non-agreement -> refused. | `FR-05` |
| `UC-04` | Request & Approve Facility Change | `ACT-01`, `ACT-02` | Driver requests move to another facility | Shipment not `CANCELLED`/`COMPLETED`/`IN_DOCK`/on-site; no other `PENDING` move | 1. Driver requests move (`120m` TTL). 2. Destination warehouse approves on `/warehouse`. 3. Destination updated, pass revoked/moved, 15m auto-hold (own) or partner request created. 4. Driver confirms -> `follow_up='BOOKED'`. | Rejected -> driver notified; no slot / hold expires unconfirmed -> `settle` reverts `destination_facility_id` and escalates. | `FR-08` |
| `UC-05` | Partner Warehouse Email Booking | `ACT-01`, `ACT-07` | Shipment bound for `is_partner=1` facility | Active partner contact with email; max 1 `PENDING_PARTNER` | 1. Request queued (`PR-XXXXXXXX`, `120m). 2. `PartnerMailRunner` sends PII-free email. 3. Partner replies `APPROVE`. 4. IMAP poll verifies contact + SPF/DKIM/DMARC + line-1 parser. 5. Atomic booking (`APT-PR-...) & driver notice. | `DECLINE` -> declined + notice; ambiguous reply / auth fail / no dock / expiry -> escalated. | `FR-09` |
| `UC-06` | Create Shipment & Confirm Dock Access Pass | `ACT-02`, `ACT-01` | Warehouse creates shipment on `/warehouse` | Open slot & labour window; eligible visible driver; valid `ORD\d{6+}` | 1. Shipment + appointment + `truck_visit(EXPECTED)` created. 2. 128-bit `DAP-...` issued (SHA-256 stored) and sent via Telegram. 3. Driver sends `DAP-...` in chat. 4. Shipment confirmed and visible in `v_driver_shipments`. | 5 wrong guesses -> shipment locked + `PASS_CONFIRMATION_LOCKED` escalation; Telegram down -> retried every 60s. | `FR-10` |
| `UC-07` | Gate Security Check-In, Unload & Check-Out | `ACT-03`, `ACT-02` | Truck arrives at gate | Live pass, `SECURITY` user scoped to facility, Redis available | 1. Guard scans/enters `DAP-...`, reg, licence -> `validate` (`VALIDATED). 2. Guard confirms `check-in` (`ACTIVE`, `CHECKED_IN`, `AT_GATE). 3. Warehouse marks `unloading-complete` (`OPERATIONS_COMPLETE). 4. Guard executes `check-out` (`CLOSED`, `CHECKED_OUT`, `COMPLETED). | Any of 8 fixed refusal codes; 10 failures/15m -> rate limited; Redis down -> 503 fail-closed; overstay -> `VISIT_OVERSTAY` handoff. | `FR-11` |
| `UC-08` | Block Dock with Displacement Planning | `ACT-02` | Maintenance or breakdown on a dock | Facility-scoped `WAREHOUSE_AUTHORITY` or `ADMIN` | 1. Preview block (`POST /dock-blocks/preview). 2. Review priority-ordered auto-move / free / skip plan. 3. Confirm block with `expected` list -> atomic block + moves + driver notices. | Concurrent booking drift -> `409 PLAN_CHANGED` without mutation; later `reopen` ends block. | `FR-07` |
| `UC-09` | Resolve Escalation Case (Web / API / CLI) | `ACT-02`, `ACT-05` | Case opened (`OPEN) | Authorized coordinator or facility-scoped staff | 1. Claim case (`CLAIMED`, driver notified). 2. Resolve case (`RESOLVED) with resolution code, note, and optional `slot_id` (web only, `ASSIGNED_SLOT). 3. Driver notified; reopen allowed within 24h. | Out-of-scope facility -> `404`; Bearer token passing `slot_id` -> refused; slot infeasible -> refused. | `FR-06` |
| `UC-10` | Admin User, Driver & Settings Governance | `ACT-04` | Admin opens `/admin` | Active `ADMIN` session | 1. Invite/disable/enable/delete users & assign facilities. 2. Approve/reject/restore drivers. 3. Save Fernet-encrypted model/LangSmith/Telegram/Email overrides & run live tests. 4. Inspect scrubbed audit log & model cost report. | Self-disable or last-admin disable -> blocked; missing `SETUHAUL_SETTINGS_KEY` -> secret save refused. | `FR-12`, `FR-13` |

---

## 4. User Stories & Acceptance Criteria

- **`US-01` (Driver Rescheduling — `UC-01)**: As a truck driver, I want to report a delay in English, Hindi, or Hinglish on Telegram and confirm a held slot with "Yes" so I do not wait on phone hold.
 - *AC1*: Given a feasible slot exists, when I report "60 mins late", then the service auto-holds rank 1 for 180s and replies in my language.
 - *AC2*: Given a live hold from a prior turn, when I reply "Yes", then my old appointment is cancelled and the new slot is confirmed in a single atomic transaction.
- **`US-02` (Decline & Cycle — `UC-01)**: As a truck driver, I want to reply "No" to a held slot so the system releases it and holds the next feasible slot.
 - *AC1*: Given a live hold, when I reply "No", then the hold is released, recorded in `offered_slot_ids`, and the next feasible slot is held; if none remain, an escalation case is opened.
- **`US-03` (Safe Cancellation — `UC-03)**: As a truck driver, I want a confirmation step before cancelling my slot so I never cancel accidentally.
 - *AC1*: Given a confirmed slot $\ge 120\text{m}$ away with penalty-free commitment, when I ask to cancel, then the bot asks for explicit confirmation and cancels only on a second-turn "Yes" within 10 min.
- **`US-04` (Concurrent Contention Safety — `UC-01)**: As a warehouse operations lead, I want competing drivers racing for the same slot to never cause a double booking.
 - *AC1*: Given 10 drivers racing for 1 slot concurrently, exactly 1 acquires the hold and 9 receive `BUSINESS_CONFLICT` with 0 invariant violations.
- **`US-05` (24/7 Slot Horizon — `FR-07)**: As a warehouse authority, I want to enter our crew count once so the system automatically maintains 1-hour slots 14 days ahead.
 - *AC1*: Given `facility_crews.crews >= 1`, `MaintenanceRunner` tops up 1-hour slots and `AUTO` labour windows for 14 days without overwriting manual labour windows.
- **`US-06` (Dock Block Preview & Commit — `UC-08)**: As a warehouse authority, I want to preview which bookings will be moved or freed before blocking a dock.
 - *AC1*: Given bookings on a dock, when I preview and confirm with `expected`, bookings move in priority order; if the board changed meanwhile, commit returns `409 PLAN_CHANGED`.
- **`US-07` (Facility Change Approval — `UC-04)**: As a destination warehouse authority, I want to approve or reject driver requests to divert to my facility.
 - *AC1*: Given a `PENDING` move, when I approve, destination updates and a 15-min hold is acquired; if unconfirmed, the shipment reverts to its original facility.
- **`US-08` (Partner Email Approval — `UC-05)**: As a partner warehouse coordinator, I want to reply "APPROVE" to a slot request email without logging into SetuHaul.
 - *AC1*: Given an authenticated email from an active contact with bare `APPROVE` on line 1, the slot is booked atomically; any ambiguous text escalates instead of booking.
- **`US-09` (Dock Access Pass & Chat Confirmation — `UC-06)**: As a warehouse authority, I want new shipments hidden from driver chat until the driver confirms the Telegram-delivered Dock Access Pass.
 - *AC1*: Given a newly created shipment, it is excluded from `v_driver_shipments` until the driver replies with the matching `DAP-...` code; 5 wrong guesses lock and escalate.
- **`US-10` (Gate Check-In/Out — `UC-07)**: As a gate security guard, I want to validate a truck's pass, registration, and licence before admitting it.
 - *AC1*: Given a valid pass within window and matching reg/licence, `validate` + `check-in` marks the visit `CHECKED_IN` and shipment `AT_GATE`; 10 failed attempts in 15 min trigger rate limiting.
- **`US-11` (Facility Isolation — `UC-10)**: As an administrator, I want warehouse and security users restricted strictly to their assigned facilities.
 - *AC1*: Given a `WAREHOUSE_AUTHORITY` bound to `FAC-JAI-01`, requesting `FAC-GGN-01` board, cases, or shipments returns `404` and records an audit event.
- **`US-12` (Runtime Configuration — `UC-10)**: As an administrator, I want to update LLM, LangSmith, Telegram, and Email settings from `/admin` without editing `.env`.
 - *AC1*: Given `SETUHAUL_SETTINGS_KEY` is configured, saved secrets are Fernet-encrypted in `app_settings`, masked in API responses, testable in-place, and revertible via "Reset to.env".

---

## 5. Feature Catalogue

| Feature Group | Included Features | Surface |
|---|---|---|
| **Driver Bot** | Trilingual chat (`en/hi/hinglish), self-contact Telegram binding, Dock Access Pass confirmation, delay/breakdown/early intake, auto-hold Yes/No cycling, two-turn cancel, facility-change request, partner slot request, GPS location sharing, driver case status lookup. | Telegram / HMAC API |
| **Warehouse Ops** | Live 3s-polled dock board, slot detail dialog & freeing, 24/7 crew-driven 14d slot top-up, dock block preview/commit/reopen, escalation case claim/resolve/assign-slot, facility-scoped driver chat viewer, direct driver messaging & failed-notice resend, facility-change approval, partner request monitor, customer projects & driver allocations, shipment creation & pass lifecycle, driver onboarding & soft-deletion. | `/warehouse` |
| **Gate Control** | QR/barcode camera & keyboard-wedge pass scanner, 8-step validation, idempotent check-in & check-out, trucks-inside board, overstay sweep. | `/gate` |
| **Admin Console** | Invite-only user lifecycle (invite, disable/enable, hybrid delete, facility bindings, password links), driver approval/suspension/restore, filtered scrubbed audit log, model usage/latency/cost dashboard + on-demand LangSmith summary, encrypted Configuration tab (Model, LangSmith, Telegram, Email) with live tests & webhook registration. | `/admin` |
| **Help & Demo** | Role-based in-app `/wiki` (5 tabs); localhost-only `/demo` console & `/demo/driver` simulator with simulated clock and in-memory partner mailbox. | `/wiki`, `/demo` |

---

## 6. Assumptions & Open Questions
- Open Questions: Confirm whether `US-01`–`US-12` cover all planned Phase 1 pilot workflows or if bulk CSV import/ERP sync is required prior to pilot (`DOC-23).
