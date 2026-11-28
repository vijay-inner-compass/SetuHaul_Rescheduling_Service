# DOC-09: Integration Architecture & API Specification

| Field | Value |
|---|---|
| **Document ID** | `DOC-09` |
| **Version** | `1.0.0` |
| **Category** | 3 — Architecture & Design |
| **Repository Commit** | `445c7f965e22e7915238cca5631e68b4462d7b7b` |
| **Validation Status** | Verified against `src/setuhaul/api.py`, `src/setuhaul/auth/*_routes.py`, and integration adapters |

---

## 1. Purpose and Scope
This document specifies the external integrations, protocol contracts, failure/retry behavior, and the complete HTTP API inventory for `SetuHaul_Rescheduling_Service`. Note that FastAPI's built-in OpenAPI/Swagger endpoints are intentionally disabled (`docs_url=None, redoc_url=None, openapi_url=None` in `src/setuhaul/api.py:500`) `[Verified]`.

## 2. Executive Summary
SetuHaul integrates with **6 external systems** (Telegram Bot API, OpenAI/Anthropic LLMs, Google OAuth2/OIDC, SMTP/IMAP Mail, Telematics/Coordinator HTTP callers, and optional LangSmith) and exposes **77 HTTP routes** across Health/Meta (3), Driver/GPS/Telegram (6), Coordinator Cases (5), Auth/Password (8), Admin API (21), Warehouse/Shipments/Exceptions API (28), Gate Security API (4), and Web Pages/Assets (8, excluding localhost-only `/demo` routes) (`[Verified]`).

---

## 3. External Integration Architecture `[Verified]`

| External System | Direction | Protocol & Auth | Timeout / Retry / Failure Mode | Prod Verification Status | Source Evidence |
|---|---|---|---|---|---|
| **Telegram Bot API** | Bidirectional | Inbound webhook (`X-Telegram-Bot-Api-Secret-Token`, $\ge 16$ chars, 256 KiB cap); outbound HTTPS `sendMessage`/`setWebhook` (`httpx`) | 5s `DriverNoticeRunner`: 5 attempts (`30s/1m/3m/5m`), 30m max age, per-driver FIFO; pass delivery retries every 60s. | `[Unverified]` (tried once via tunnel with stub model) | `auth/telegram_routes.py:137-177`, `jobs/send_driver_notifications.py:27-117`, `webhook_startup.py` |
| **LLM Providers (OpenAI / Anthropic)** | Outbound | HTTPS JSON (`api.anthropic.com/v1/messages` or OpenAI `chat/completions` / configurable `SETUHAUL_OPENAI_BASE_URL`) | `8s` timeout, `1` retry on 429/5xx/timeout, `30s` deadline, `6` rounds, `$0.05` cap; falls back to `MODEL_UNAVAILABLE` escalation. | `[Unverified]` post-11/27 changes | `model_gateway.py:274-501` |
| **Google OAuth2 / OIDC** | Bidirectional | Authorization Code + PKCE (`S256`) + `nonce`; signed `setuhaul_oauth` cookie + Redis `auth:state:{state}` (600s) | Fails closed (`503`) if Redis unavailable; relies on direct TLS token endpoint (no JWKS verify, `D73`). | `[Unverified]` (never run against real Google) | `auth/google.py:77-105`, `auth/routes.py:164-245` |
| **SMTP / IMAP Mail** | Bidirectional | Outbound `SMTP_SSL` (:465) or `STARTTLS`; inbound `IMAP4_SSL` (:993) `BODY.PEEK[]` search `SINCE 3d SUBJECT "SetuHaul"` | Partner send: `MAX_ATTEMPTS=5`; reply requires active contact + SPF/DKIM/DMARC `Authentication-Results` + bare `APPROVE`/`DECLINE`. Account mail: invite (48h) / reset (1h). | `[Unverified]` (never run against real mailbox) | `mail_transport.py:86-183`, `partner_booking.py:302-348`, `auth/account_mail.py` |
| **Redis 7** | Outbound | RESP over TCP (`1.0s` connect/read timeout) | Cache/driver-rate-limit fail-open on `RedisError`; OAuth state & Gate rate-limit fail-closed (`503`); Auth rate-limit uses in-process fallback. | `[Unverified]` (tested with `fakeredis` only) | `api.py:1213-1219`, `rate_limit.py:51-157`, `memory.py:315-358` |
| **LangSmith (Optional)** | Outbound | HTTPS (`api.smith.langchain.com`); triple opt-in (`ENABLED=true` + `approved` + API key) | PII/secret scrubbing + optional `HIDE_TEXT=1`; fail-closed on unserializable objects; upload warnings throttled to 1/600s; report `5s` timeout, 0 retries. | `[Unverified]` in prod | `langsmith_trace.py:51-232`, `langsmith_report.py` |
| **Downstream Planner / Consignee / Ops** | Outbound (planned) | `notification_outbox` rows (`WAREHOUSE_PLANNER`, `CONSIGNEE`, `OPERATIONS`) | **No production sender connected** (`TD-03`); rows remain `PENDING`. | `[Missing]` | `notifications.py:63-98`, `jobs/deliver_notifications.py:3-6` |

*(Partner & Facility Change sequence diagram: [`docs/enterprise/diagrams/06-seq-partner-and-facility-change.mmd`](../diagrams/06-seq-partner-and-facility-change.mmd))*

---

## 4. Complete HTTP API Specification `[Verified]`

### 4.1 Health, Readiness & Metadata (`src/setuhaul/api.py`)
| Method | Path | Auth | Behavior |
|---|---|---|---|
| `GET` | `/health` | Public | Returns `{"status": "ok"}`. |
| `GET` | `/version` | Public | Returns `GIT_COMMIT` env var or `"unknown"`. |
| `GET` | `/ready`, `/health/ready` | Public | Isolated `CapacityLimiter(2)`: checks SQLite `schema_migrations` (`503` if down) and Redis `ping()` (`200` with `degraded: ["cache"]` if down). |

### 4.2 Driver Channel, GPS & Telegram (`api.py`, `auth/telegram_routes.py`)
| Method | Path | Auth & Limits | Purpose |
|---|---|---|---|
| `POST` | `/v1/driver/messages` | HMAC-SHA256 (`X-Signature`), 64 KiB cap, eligible driver, Redis rate limit (`20/m`, `100/h`) | Inbound driver message -> idempotency -> pass check -> `handle_message()`. |
| `POST` | `/v1/gps/pings` | HMAC-SHA256, 16 KiB cap | Telematics GPS ping (`<= +300s` clock skew) -> `store_ping` + `handle_new_position`. |
| `POST` | `/v1/driver/location` | HMAC-SHA256, 16 KiB cap, eligible driver | Driver-shared location -> `store_ping` + `handle_driver_position`. |
| `POST` | `/v1/driver/status` | HMAC-SHA256 + `±300s` `ts` replay window, 16 KiB cap | Driver escalation case status & 10 recent exceptions. |
| `GET` | `/v1/driver/status` | HMAC-SHA256 over `phone\|ts` + `±300s` | **Deprecated** (`Deprecation: true` header; exposes phone in query string). |
| `POST` | `/v1/telegram/webhook` | `X-Telegram-Bot-Api-Secret-Token`, 256 KiB cap | Private Telegram updates: self-contact binding or bound-driver chat. |

### 4.3 Coordinator Escalation API (`api.py:966-1078`)
Auth: Coordinator Bearer token (unscoped) OR Web Session (`WAREHOUSE_AUTHORITY`/`ADMIN`, facility-scoped; `404` out of scope).
- `GET /v1/coordinator/cases` (default `OPEN,CLAIMED`, cap 200)
- `GET /v1/coordinator/cases/{case_id}` (detail + `case_events`)
- `POST /v1/coordinator/cases/{case_id}/claim`
- `POST /v1/coordinator/cases/{case_id}/resolve` (optional `slot_id` requires Session user + `ASSIGNED_SLOT`)
- `POST /v1/coordinator/cases/{case_id}/reopen` (within 24h window)

### 4.4 Authentication & Password Routes (`auth/routes.py`, `auth/password_routes.py`)
- `GET /auth/login` (Google OIDC+PKCE redirect; `503` if unconfigured)
- `GET /auth/callback` (OIDC callback; Redis single-use state; sets 8h `setuhaul_session`)
- `POST /auth/local-admin` (Break-glass admin login; `404` if `SETUHAUL_LOCAL_ADMIN!=1`; 1h session)
- `POST /auth/logout` (Revokes session, audits `LOGOUT`, clears cookie)
- `GET /auth/me` (Returns `{email, role, display_name, facilities}` or `401`)
- `POST /auth/password` (JSON email/password login; `404` if disabled; 8h session)
- `POST /auth/password/set` (Redeem 48h `INVITE` or 1h `RESET` token; revokes all sessions)
- `POST /auth/password/forgot` (Constant neutral reply + async reset email)

### 4.5 Admin API (`ADMIN` Session Required)
- **Runners & Audit**: `GET /v1/admin/runners`, `GET /v1/admin/audit` (`limit <= 500`, scrubbed), `GET /v1/admin/model/langsmith`
- **Users**: `POST /v1/admin/users` (invite), `GET /v1/admin/users`, `POST /v1/admin/users/{id}/disable`, `POST /v1/admin/users/{id}/enable`, `POST /v1/admin/users/{id}/delete` (hybrid hard/soft), `POST /v1/admin/users/{id}/facilities`, `POST /v1/admin/users/{id}/password-link`
- **Drivers**: `GET /v1/drivers/pending`, `POST /v1/drivers/{id}/approve`, `POST /v1/drivers/{id}/reject`, `POST /v1/drivers/{id}/restore`
- **Runtime Settings**: `GET /v1/admin/settings` (masked), `POST /v1/admin/settings/{section}` (`model|langsmith|telegram|email`), `POST /v1/admin/settings/{section}/reset`, `POST /v1/admin/settings/model/test`, `POST /v1/admin/settings/telegram/test`, `POST /v1/admin/settings/telegram/register-webhook`, `POST /v1/admin/settings/email/test`

### 4.6 Warehouse, Shipments, Exceptions & Projects API (`WAREHOUSE_AUTHORITY` or `ADMIN`, Facility-Scoped)
- **Board & Slots**: `GET /v1/warehouse/board`, `GET /v1/warehouse/open-slots` (`limit <= 500`), `POST /v1/warehouse/auto-slots`, `POST /v1/warehouse/appointments/{id}/free`
- **Dock Blocks**: `POST /v1/warehouse/dock-blocks/preview`, `POST /v1/warehouse/dock-blocks` (`409 PLAN_CHANGED`), `POST /v1/warehouse/dock-blocks/{id}/reopen`
- **Chats, Messages & Requests**: `GET /v1/warehouse/driver-chats`, `POST /v1/warehouse/drivers/{id}/messages`, `POST /v1/warehouse/notices/{id}/resend`, `GET /v1/warehouse/partner-requests`, `GET /v1/warehouse/facility-changes`, `POST /v1/warehouse/facility-changes/{id}/approve`, `POST /v1/warehouse/facility-changes/{id}/reject`
- **Shipments & Passes**: `GET /v1/warehouse/shipments`, `POST /v1/warehouse/availability`, `POST /v1/warehouse/shipments`, `POST /v1/warehouse/shipments/{id}/driver`, `POST /v1/warehouse/shipments/{id}/pass`, `POST /v1/warehouse/shipments/{id}/pass/revoke`, `POST /v1/warehouse/shipments/{id}/cancel`, `POST /v1/warehouse/shipments/{id}/unlock`, `POST /v1/warehouse/visits/{id}/unloading-complete`
- **Drivers & Projects**: `POST /v1/drivers`, `GET /v1/warehouse/drivers`, `POST /v1/warehouse/drivers/{id}/telegram/revoke`, `POST /v1/warehouse/drivers/{id}/telegram/allow-relink`, `GET /v1/warehouse/drivers/{id}/deletion`, `POST /v1/warehouse/drivers/{id}/delete`, `POST /v1/projects`, `GET /v1/projects`, `POST /v1/projects/{id}/allocate`, `POST /v1/projects/{id}/release`

### 4.7 Gate Security API (`SECURITY` or `ADMIN`, Facility-Scoped, Fail-Closed on Redis Outage)
- `POST /v1/gate/validate`
- `POST /v1/gate/check-in` (idempotent via `request_key`)
- `POST /v1/gate/check-out` (idempotent via `request_key`)
- `GET /v1/gate/visits`

### 4.8 Web Pages & Static Assets
- `GET /`, `GET /forgot-password`, `GET /set-password`, `GET /admin`, `GET /warehouse`, `GET /gate`, `GET /wiki`, `GET /app/assets/{name}` (11 whitelisted files).
- Localhost-only demo routes (`/demo`, `/demo/api/*`, `/demo/login`, `/demo/driver*`) are registered solely by `scripts/run_demo.py`.

---

## 5. Source Evidence, Limitations & Open Questions
- Evidence: `src/setuhaul/api.py`, `src/setuhaul/auth/*_routes.py`, `src/setuhaul/mail_transport.py`, `src/setuhaul/model_gateway.py`.
- Limitations: Deprecated `GET /v1/driver/status` remains live (`TD-11`); Coordinator Bearer tokens bypass facility scoping and require restart to rotate (`api.py:966-970`).
- Open Questions: When can `GET /v1/driver/status` be retired? What transport should consume `WAREHOUSE_PLANNER`/`CONSIGNEE`/`OPERATIONS` outbox rows (`Q8`)?
