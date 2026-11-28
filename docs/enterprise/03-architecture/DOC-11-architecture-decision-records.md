# DOC-11: Architecture Decision Records (ADRs)

| Field | Value |
|---|---|
| **Document ID** | `DOC-11` |
| **Version** | `1.0.0` |
| **Category** | 3 — Architecture & Design |
| **Repository Commit** | `445c7f965e22e7915238cca5631e68b4462d7b7b` |
| **Validation Status** | Synthesized from `docs/decision-register.md` (D1–D107) and verified commit history |

---

## 1. Purpose and Scope
This document consolidates the 107 repository decisions (`D1`–`D107` in `docs/decision-register.md`) plus undocumented recent commits into formal enterprise Architecture Decision Records (`ADR-01`–`ADR-12`), explicitly tracking superseded decisions and open owner confirmations.

## 2. Executive Summary
The repository maintains an unusually disciplined decision log (`docs/decision-register.md`, D1–D107). Key architectural pivots include replacing the numbered 3-option menu (`D65`) with auto-hold one-slot confirmation (`D93`), replacing project-window chat visibility (`D86`) with per-message load tagging (`D104`), replacing order-number verification (`D100`) with 128-bit Dock Access Passes (`D101`), and adding hybrid web-user soft-deletion (`migrations/027_user_deletions.sql`, undocumented in `decision-register.md` -> `GAP-04`) (`[Verified]`).

---

## 3. Consolidated Architecture Decision Records `[Verified]`

| ADR ID | Decision Title | Source Refs | Status | Context, Decision & Consequences |
|---|---|---|---|---|
| `ADR-01` | Deterministic Core + Bounded LLM Boundary | `D13, D38, D53, D58, D60` | Accepted | **Decision**: LLM (LangGraph behind provider-neutral `ModelGateway`) only interprets language and calls 14 narrow Pydantic tools; deterministic Python + SQLite own identity, ETA, feasibility, holds, confirms, and rendered replies. Hard caps (`6` rounds, `4` tools, `8s` timeout, `30s` deadline, `$0.05` budget) run outside the graph. **Consequence**: Eliminates hallucinated bookings; requires maintaining trilingual templates. |
| `ADR-02` | SQLite WAL System of Record + Seed Immutability | `D3, D14, D24, D97` | Accepted (Prototype) | **Decision**: Use bundled SQLite in WAL mode with `BEGIN IMMEDIATE` and `5000ms` busy timeout; SHA-256 pin `data/setuhaul.sqlite3` and refuse live execution against it. **Consequence**: Zero external DB ops for prototype; caps scaling at 1 writer (~244 holds/s) until PostgreSQL migration. |
| `ADR-03` | Holds Inside `appointments` Sharing Unique Index | `D24, D25, D28` | Accepted | **Decision**: Store holds as `appointments(status='HELD', is_current=0)` guarded by `ux_active_appointment_per_slot` and `ux_one_hold_per_shipment`; confirm via single atomic swap. **Consequence**: Database constraint makes double-booking impossible across all channels and planners. |
| `ADR-04` | Fail-Closed Feasibility & Strict Operational IST | `D6, D8, D10, D18, D19` | Accepted | **Decision**: Require offset-aware timestamps normalized to `Asia/Kolkata`. Missing labour or consignee records return `INCOMPLETE_FEASIBILITY_DATA` (never assumed). Unload duration must fit a single slot. **Consequence**: Prevents unsafe promises; blocks auto-booking on supplied snapshot without fixtures (`Q17`, `Q20`). |
| `ADR-05` | Auto-Hold One-Slot Flow Supersedes Menu | `D65 -> D93` | Accepted (Supersedes `D65`) | **Decision**: Replaced 3-option numbered menu with auto-holding rank-1 feasible slot (`Yes` confirms, `No` releases and holds next). Added destination-approved facility change with auto-revert. **Consequence**: Faster driver UX; requires careful `writes_done` accounting (`TD-05`). |
| `ADR-06` | GPS Arrival Floor & Proactive Earlier-Slot Offer | `D67, D68` | Accepted | **Decision**: Fresh GPS pings ($\le 15\text{m}$) act strictly as a lower bound on arrival (never overriding a driver's later ETA), release unreachable holds, and proactively hold $\ge 30\text{m}$ earlier slots (`900s` TTL). **Consequence**: Protects against unrealistic ETAs; persists `eta_buffer_minutes=15` into option context (`TD-06`). |
| `ADR-07` | 24/7 Rolling 14-Day Horizon Driven by Crew Counts | `D105` | Accepted | **Decision**: Open facilities 24/7 (`00:00–00:00`, disable `LAST_NEW_START_TIME`) and auto-generate 1-hour slots + `AUTO` labour windows 14 days ahead ONLY where staff enter `facility_crews.crews >= 1`. **Consequence**: Preserves fail-closed labour rule while removing manual daily slot creation. |
| `ADR-08` | Partner Warehouse Email Approval Protocol | `D69, D70, D71` | Accepted | **Decision**: Coordinate partner facilities (`is_partner=1`) via PII-free SMTP/IMAP email requiring active contact, SPF/DKIM/DMARC `Authentication-Results` (`mx.google.com`), and bare `APPROVE`/`DECLINE` on line 1. **Consequence**: Safe without partner portal login; `[Unverified]` against live mail servers. |
| `ADR-09` | 128-Bit Dock Access Pass & Gate Security Role | `D100 -> D101` | Accepted (Supersedes `D100` order-number check) | **Decision**: Replace order-number verification with 128-bit `DAP-...` (SHA-256 stored) delivered via Telegram, confirmed once in chat (5-failure lock), and verified by `SECURITY` role at `/gate`. **Consequence**: Stronger physical & chat access control; requires Redis availability at gate (fail-closed). |
| `ADR-10` | Invite-Only Multi-Mode Auth & Facility-Scoped RBAC | `D73–D78, D82, D103, D107` | Accepted | **Decision**: Server-side SHA-256 sessions in SQLite; invite-only Google OIDC+PKCE (no JWKS verify, `D73`), admin-toggleable email/password login (`D107`), break-glass local admin (`scrypt`, per-IP + global lockout), SameSite=Lax + same-origin CSRF, facility scoping (`404` out-of-scope). **Consequence**: Strong tenant/facility isolation; Google OAuth `[Unverified]` live. |
| `ADR-11` | Per-Message Load Scoping for Warehouse Chats | `D86 -> D104` | Accepted (Supersedes `D86`) | **Decision**: Tag chat messages in `chat_message_loads(chat_message_id, shipment_id, facility_id)` rather than inferring visibility from project allocation windows. **Consequence**: Accurate facility chat scoping even when drivers span projects. |
| `ADR-12` | Hybrid Hard/Soft Web User Deletion | Commits `e430ec3`, `f8e106f` (`027_user_deletions.sql`) | Implemented (**Undocumented in `decision-register.md`: `GAP-04`**) | **Decision**: Hard-delete unreferenced web users; soft-delete historically referenced users by disabling, rewriting email to `deleted+<id>@deleted.invalid`, and logging `original_email` in `user_deletions`. **Consequence**: Preserves FK/audit integrity while freeing email addresses for re-invitation. |

---

## 4. Open Decisions Requiring Owner Confirmation `[Verified]`
From `docs/decision-register.md` and `docs/operations-questions.md`: `D15` (driver status eligibility), `D42` (24h case reopen window), `D78` (admin project allocation), `D88` (sticky Telegram revoke), and all 6 `BLOCKER` operational questions (`Q3, Q4, Q6, Q9, Q17, Q20`).

## 5. Source Evidence
- `docs/decision-register.md:5-111`, `docs/decision-answers.md:1-143`, `migrations/027_user_deletions.sql`, `src/setuhaul/auth/users.py:189-263`.
