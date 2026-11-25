# Authentication, Roles and Driver Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Google-only web sign-in for admins and warehouse authorities, drivers authenticated by Telegram (bound by shared contact), driver onboarding with admin approval, contract projects with driver allocation, role-specific screens, and an append-only audit log.

**Architecture:** New extension tables (migration 011) and small focused modules under `src/setuhaul/auth/`. A server-side Google authorization-code + PKCE flow issues HttpOnly cookie sessions stored as `sha256(token)`, like `driver_sessions`. One `require_role` dependency guards every route; facility and project scope is applied inside queries. Google is replaced by an injectable verifier in tests.

**Tech Stack:** Python 3.12, FastAPI, SQLite migrations, stdlib `hashlib.scrypt` and `secrets`, `httpx` (already a dependency) for Google's token and keys endpoints and the Telegram Bot API, pytest + fakeredis, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-11-24-auth-roles-design.md`

## Global Constraints

- Google sign-in only; the only local credential is the optional admin password, off unless `SETUHAUL_LOCAL_ADMIN=1`.
- Invite-only: an email not in `users` (or `DISABLED`) is refused, creates no row and no session, and is audited.
- Sessions: 8 hours (Google), 1 hour (break-glass). Cookie `HttpOnly`, `SameSite=Lax`, `Secure` except on localhost. Stored as `sha256(token)`.
- Break-glass lockout: 5 failures lock for 15 minutes; every attempt audited.
- Telegram: webhook requires header `X-Telegram-Bot-Api-Secret-Token` equal to `SETUHAUL_TELEGRAM_WEBHOOK_SECRET` (constant-time compare). A contact binds only when `message.contact.user_id == message.from.id`, the normalised E.164 number matches an `APPROVED` driver with a valid licence, and that driver has no active binding. Refusals are neutral and audited.
- `drivers`, `vehicles`, `carriers` are insert-only (supplied rows never changed). New drivers insert as `INACTIVE`; approval sets `ACTIVE` only for rows whose `driver_profiles.created_by_user_id` is set.
- `audit_log` is append-only (triggers reject UPDATE and DELETE) and never holds passwords, tokens or codes.
- Web roles: `ADMIN`, `WAREHOUSE_AUTHORITY`. Drivers are never users and have no web login. A driver never receives dock board data; the driver's `driver_id` always comes from the bound Telegram account, never from message content or request parameters.
- Env vars: `SETUHAUL_GOOGLE_CLIENT_ID`, `SETUHAUL_GOOGLE_CLIENT_SECRET`, `SETUHAUL_SESSION_SECRET`, `SETUHAUL_BOOTSTRAP_ADMIN_EMAIL`, `SETUHAUL_LOCAL_ADMIN`, `SETUHAUL_TELEGRAM_BOT_TOKEN`, `SETUHAUL_TELEGRAM_WEBHOOK_SECRET`.
- Follow the codebase style: comment on each logical step, lazy annotations (`from __future__ import annotations`), and pass `ruff`, `mypy`, and `scripts/check_comment_only.py`.

## Review Focus

- Email case differences (`Vijay@X.com` vs `vijay@x.com`) must match one user (Task 2 test).
- Google `email_verified=false` or wrong `aud`/`iss`/expired token must be refused (Task 4 tests).
- A replayed or tampered `state` on `/auth/callback` must be refused (Task 4 test).
- A disabled user's existing sessions must stop working immediately (Task 3 test).
- A driver whose licence expires after approval must lose bot access without any admin action (Task 7 and 8 tests).
- Phone numbers in many formats (`+91 90000 10006`, `09000010006`, `919000010006`) must match one driver; a forwarded or someone else's contact must never bind (Task 8 tests).
- Telegram updates with a missing or wrong secret header, a non-text message, or an unbound sender must not reach the engine (Task 8 tests).

---

### Task 1: Migration 011 and the audit log

**Files:**
- Create: `migrations/011_auth_roles.sql`, `src/setuhaul/auth/__init__.py`, `src/setuhaul/auth/audit.py`
- Test: `tests/test_auth_audit.py`

**Interfaces:**
- Produces: tables `users` (role only `ADMIN` or `WAREHOUSE_AUTHORITY`), `user_facilities`, `auth_sessions`, `admin_credentials`, `driver_profiles`, `telegram_identities`, `projects`, `project_drivers`, `audit_log` (columns exactly as in the spec). `audit.record(conn, *, actor_user_id: str | None, actor_email: str | None, action: str, target_type: str | None, target_id: str | None, outcome: str, ip: str | None, detail: dict[str, object] | None, clock: Clock) -> None` and `audit.query(conn, *, actor: str | None = None, action: str | None = None, outcome: str | None = None, since: str | None = None, limit: int = 200) -> list[sqlite3.Row]`.

- [ ] **Step 1: Write failing tests** in `tests/test_auth_audit.py`: `test_record_then_query_roundtrip`; `test_update_is_rejected` and `test_delete_is_rejected` (both expect `sqlite3.IntegrityError` from the triggers); `test_detail_never_stores_secret_keys` (keys named `password`, `token`, `code` are dropped before writing).
- [ ] **Step 2: Run** `pytest tests/test_auth_audit.py -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Write `migrations/011_auth_roles.sql`** with the nine tables, `CHECK` constraints for role/status/approval values, `users.email` as `TEXT UNIQUE COLLATE NOCASE`, and `BEFORE UPDATE`/`BEFORE DELETE` triggers on `audit_log` that `RAISE(ABORT, 'audit_log is append-only')`.
- [ ] **Step 4: Implement `audit.py`** per the signatures; `record` writes JSON detail after dropping the forbidden keys.
- [ ] **Step 5: Run** `pytest tests/test_auth_audit.py tests -q -x`. Expected: PASS (existing migration tests still apply 011 cleanly).
- [ ] **Step 6: Commit** `feat: auth schema (migration 011) and append-only audit log`.

### Task 2: Users, invites and bootstrap admin

**Files:**
- Create: `src/setuhaul/auth/users.py`
- Modify: `docs/writable-data-contract.md`, `docs/decision-register.md` (insert-only change; new tables listed)
- Test: `tests/test_auth_users.py`

**Interfaces:**
- Consumes: `audit.record` (Task 1).
- Produces: `Role = Literal["ADMIN","WAREHOUSE_AUTHORITY"]`; `User` frozen model (`user_id, email, role, status, display_name, facilities: tuple[str, ...]`); `users.invite(conn, *, actor: User, email: str, role: Role, display_name: str, facilities: tuple[str, ...] = (), clock) -> User` (ADMIN only; raises `AuthError("FORBIDDEN")` otherwise); `users.find_by_email(conn, email: str) -> User | None`; `users.disable(conn, *, actor: User, user_id: str, clock) -> None` (also sets `revoked_at` on all that user's sessions); `users.bootstrap_admin(conn, email: str, clock) -> User` (idempotent).

- [ ] **Step 1: Write failing tests:** `test_invite_requires_admin`; `test_email_match_is_case_insensitive` (invite `Vijay@X.com`, find by `vijay@x.com`); `test_bootstrap_admin_is_idempotent`; `test_disable_revokes_sessions` (inserts an `auth_sessions` row directly, asserts `revoked_at` set); `test_invite_writes_audit_row`.
- [ ] **Step 2: Run** `pytest tests/test_auth_users.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement `users.py`** per the signatures. Warehouse authority invites require at least one facility that exists in `facilities`.
- [ ] **Step 4: Update the two docs** to state insert-only writes for `drivers`, `vehicles`, `carriers` and the new extension tables.
- [ ] **Step 5: Run** `pytest tests/test_auth_users.py -v`. Expected: PASS.
- [ ] **Step 6: Commit** `feat: invite-only users and bootstrap admin; data contract allows insert-only driver tables`.

### Task 3: Sessions and cookies

**Files:**
- Create: `src/setuhaul/auth/sessions.py`
- Test: `tests/test_auth_sessions.py`

**Interfaces:**
- Consumes: `users.User`, `audit.record`.
- Produces: `sessions.create(conn, user: User, method: Literal["GOOGLE","LOCAL_ADMIN"], clock) -> str` (returns the raw token; stores only its sha256; TTL 8h or 1h); `sessions.resolve(conn, token: str, clock) -> User | None` (None if unknown, expired, revoked, or user `DISABLED`); `sessions.revoke(conn, token: str, clock) -> None`; `COOKIE_NAME = "setuhaul_session"`; `sessions.cookie_kwargs(secure: bool) -> dict[str, object]`.

- [ ] **Step 1: Write failing tests:** `test_resolve_valid_token`; `test_expired_session_is_none` (advance `FakeClock` 8h01m); `test_break_glass_session_expires_after_one_hour`; `test_revoked_is_none`; `test_disabled_user_session_is_none`; `test_raw_token_is_not_stored` (the raw token appears nowhere in `auth_sessions`).
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** with `secrets.token_urlsafe(32)` and `hashlib.sha256`.
- [ ] **Step 4: Run** `pytest tests/test_auth_sessions.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: hashed cookie sessions with expiry and revocation`.

### Task 4: Google sign-in routes

**Files:**
- Create: `src/setuhaul/auth/google.py`, `src/setuhaul/auth/routes.py`
- Modify: `src/setuhaul/api.py` (add `google: GoogleVerifier | None` and `session_secret: bytes` to `AppDeps`; call `register_auth_routes(app, deps)` inside `create_app`; read the new env vars in `create_app_from_env`)
- Test: `tests/test_auth_google.py`

**Interfaces:**
- Consumes: `users.find_by_email`, `sessions.create`, `audit.record`.
- Produces: `GoogleVerifier` protocol: `authorize_url(state: str, code_challenge: str, redirect_uri: str) -> str` and `exchange_and_verify(code: str, code_verifier: str, redirect_uri: str) -> GoogleIdentity` where `GoogleIdentity(email: str, email_verified: bool, name: str)`; raises `AuthError("BAD_TOKEN")` on wrong `iss`, `aud` or expiry. `RealGoogleVerifier(client_id, client_secret, http: httpx.Client)` and `FakeGoogleVerifier` (tests). Routes: `GET /auth/login`, `GET /auth/callback`, `POST /auth/logout`, `GET /auth/me` (returns role and email).
- State and PKCE verifier travel in a signed short-lived cookie (HMAC with `session_secret`, 10-minute life).

- [ ] **Step 1: Write failing tests** with `FakeGoogleVerifier`: `test_login_redirects_with_state_and_challenge`; `test_callback_invited_user_gets_cookie`; `test_callback_uninvited_email_refused_no_user_row_no_session`; `test_callback_unverified_email_refused`; `test_callback_tampered_state_refused`; `test_callback_replayed_state_refused` (second use of the same state cookie fails); `test_real_verifier_rejects_wrong_audience_and_expired_token` (inject a canned token payload); `test_logout_revokes`; `test_every_outcome_is_audited` (LOGIN_OK, LOGIN_REFUSED).
- [ ] **Step 2: Run** `pytest tests/test_auth_google.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the verifier (fetch Google's signing keys via `httpx`, verify RS256 JWT with the stdlib-free approach already available; if no JWT library is installed, add `PyJWT[crypto]` pinned to an exact version in `pyproject.toml` and `docs/requirements-lock.txt`) and the routes.
- [ ] **Step 4: Run** `pytest tests/test_auth_google.py tests/test_api.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: Google sign-in (code flow with PKCE), invite-only`.

### Task 5: Break-glass admin password

**Files:**
- Create: `src/setuhaul/auth/local_admin.py`, `scripts/set_admin_password.py`
- Modify: `src/setuhaul/auth/routes.py` (add `POST /auth/local-admin`)
- Test: `tests/test_auth_local_admin.py`

**Interfaces:**
- Consumes: `sessions.create(..., method="LOCAL_ADMIN")`.
- Produces: `local_admin.set_password(conn, user_id: str, password: str) -> None` (scrypt, per-user salt, minimum length 12); `local_admin.verify(conn, email: str, password: str, clock) -> User` (raises `AuthError("LOCKED")` / `AuthError("BAD_CREDENTIALS")`; only ADMIN users).

- [ ] **Step 1: Write failing tests:** `test_disabled_by_default` (route returns 404 unless `SETUHAUL_LOCAL_ADMIN=1`); `test_correct_password_gives_one_hour_session`; `test_five_failures_lock_for_15_minutes_then_unlock` (FakeClock); `test_non_admin_cannot_use_it`; `test_no_password_or_hash_in_audit_detail`; `test_every_attempt_audited`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** with `hashlib.scrypt` and `hmac.compare_digest`; the CLI prompts with `getpass`, never takes the password as an argument.
- [ ] **Step 4: Run** `pytest tests/test_auth_local_admin.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: audited, rate-limited break-glass admin password`.

### Task 6: Role guard and facility scope

**Files:**
- Create: `src/setuhaul/auth/authz.py`
- Test: `tests/test_auth_authz.py`

**Interfaces:**
- Consumes: `sessions.resolve`, `COOKIE_NAME`.
- Produces: `require_role(*roles: Role) -> Callable[[Request], User]` (FastAPI dependency; 401 with code `UNAUTHENTICATED` when no valid session, 403 `FORBIDDEN` when the role is wrong); `facility_ids(user: User) -> tuple[str, ...] | None` (None means all facilities, for ADMIN); `assert_facility(user: User, facility_id: str) -> None` (raises 403).

- [ ] **Step 1: Write failing tests** on a tiny app with one route per role: `test_no_cookie_is_401`; `test_wrong_role_is_403`; `test_right_role_is_200`; `test_warehouse_scoped_to_own_facilities`; `test_admin_sees_all_facilities`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** per signatures.
- [ ] **Step 4: Run** `pytest tests/test_auth_authz.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: server-side role guard and facility scope`.

### Task 7: Driver onboarding and approval

**Files:**
- Create: `src/setuhaul/auth/onboarding.py`
- Create: `src/setuhaul/auth/admin_routes.py` for `POST /v1/drivers`, `GET /v1/drivers/pending`, `POST /v1/drivers/{driver_id}/approve`, `POST /v1/drivers/{driver_id}/reject`
- Test: `tests/test_auth_onboarding.py`

**Interfaces:**
- Consumes: `authz.require_role`. Drivers are not users; no invite is created.
- Produces: `onboarding.add_driver(conn, *, actor: User, name: str, phone: str, licence_number: str, licence_expiry: date, carrier_id: str, vehicle_id: str, clock) -> str` (returns `driver_id`; inserts `drivers` row `INACTIVE` plus `driver_profiles` `PENDING_APPROVAL`; duplicate phone or licence returns a clear error); `onboarding.approve(conn, *, actor: User, driver_id: str, clock) -> None` (ADMIN; sets `drivers.driver_status='ACTIVE'` and `APPROVED`); `onboarding.reject(...)`; `onboarding.licence_valid(conn, driver_id: str, today: date) -> bool`.

- [ ] **Step 1: Write failing tests:** `test_warehouse_adds_driver_pending_and_inactive`; `test_only_admin_approves`; `test_supplied_driver_rows_unchanged_by_onboarding` (compare the 15 original rows before and after); `test_duplicate_licence_refused`; `test_expired_licence_blocks_approval`; `test_licence_expiring_after_approval_fails_licence_valid` (FakeClock past expiry, no admin action); `test_add_and_approve_are_audited`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** in one `immediate()` transaction per operation; generate `driver_id` as `DRV` + next number above the supplied ones.
- [ ] **Step 4: Run** `pytest tests/test_auth_onboarding.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: driver onboarding with admin approval and licence rules`.

### Task 8: Telegram channel and contact binding

**Files:**
- Create: `src/setuhaul/auth/telegram.py`, `src/setuhaul/auth/phone.py`, `src/setuhaul/auth/telegram_routes.py`
- Modify: `src/setuhaul/api.py` (add `telegram_secret: bytes` and `telegram: TelegramClient | None` to `AppDeps`; register the route; read env vars in `create_app_from_env`)
- Test: `tests/test_telegram_binding.py`, `tests/test_telegram_channel.py`

**Interfaces:**
- Consumes: the conversation entry point the existing `/v1/driver/messages` route calls (read `api.py` near line 422 and reuse it unchanged), `onboarding.licence_valid`, `audit.record`.
- Produces: `phone.normalise_e164(raw: str, default_country: str = "91") -> str | None` (None when not a plausible number); `telegram.bind_from_contact(conn, *, telegram_user_id: int, chat_id: int, contact_user_id: int | None, contact_phone: str, clock) -> BindResult` (`BOUND | REFUSED`; the reason goes in the audit row only, never to the driver); `telegram.resolve_driver(conn, telegram_user_id: int, today: date) -> str | None` (None unless bound, not revoked, driver `APPROVED` and licence valid); `telegram.revoke(conn, *, actor: User, driver_id: str, clock) -> None`; `TelegramClient.send(chat_id: int, text: str, request_contact_button: bool = False) -> None` (real one uses `httpx` and the bot token; a fake records messages). Route: `POST /v1/telegram/webhook`.
- Behaviour: an update from an unbound sender gets one neutral reply with a "Share my phone number" button; a contact message runs `bind_from_contact`; a bound driver's text goes to the existing engine with the resolved `driver_id`; everything else gets the neutral refusal and nothing reaches the engine.

- [ ] **Step 1: Write failing tests** in `tests/test_telegram_binding.py`: `test_normalise_formats` (`+91 90000 10006`, `09000010006`, `919000010006`, `9000010006` all give `+919000010006`; `abc` gives None); `test_contact_of_sender_binds_approved_driver`; `test_forwarded_contact_with_other_user_id_refused`; `test_contact_without_user_id_refused`; `test_pending_or_rejected_driver_refused`; `test_expired_licence_refused`; `test_unregistered_phone_gets_same_reply_as_wrong_user`; `test_second_telegram_account_for_same_driver_refused`; `test_revoked_binding_refused_and_rebind_allowed_only_when_approved`; `test_bind_and_refusals_audited`.
- [ ] **Step 2: Write failing tests** in `tests/test_telegram_channel.py`: `test_missing_or_wrong_secret_header_is_401`; `test_unbound_sender_gets_share_contact_prompt_and_engine_not_called`; `test_bound_driver_message_reaches_engine_with_bound_driver_id` (a message saying "I am DRV002" still uses the bound id); `test_non_text_update_is_ignored_safely`; `test_licence_expiring_after_binding_stops_access` (FakeClock); `test_driver_reply_is_sent_to_bound_chat`.
- [ ] **Step 3: Run** `pytest tests/test_telegram_binding.py tests/test_telegram_channel.py -v`. Expected: FAIL.
- [ ] **Step 4: Implement** per signatures. Use `hmac.compare_digest` for the secret header; run the engine call through `run_in_threadpool` like the existing route; insert into `telegram_identities` in one `immediate()` transaction (the `UNIQUE(driver_id)` constraint enforces one account per driver).
- [ ] **Step 5: Check delivery:** read how queued driver notifications (`notification_outbox`, recipient `DRIVER`) are sent today and route them to the bound chat through `TelegramClient`. If no sender exists, add `src/setuhaul/jobs/send_driver_notifications.py` modelled on `jobs/send_partner_requests.py`, with a test. If the outbox `kind` CHECK blocks any needed message, add a rebuild migration `012_*.sql`.
- [ ] **Step 6: Run** `pytest tests -q`. Expected: PASS.
- [ ] **Step 7: Commit** `feat: Telegram driver channel bound by shared contact`.

### Task 9: Projects and allocation

**Files:**
- Create: `src/setuhaul/auth/projects.py`
- Modify: `src/setuhaul/auth/admin_routes.py` (`POST /v1/projects`, `GET /v1/projects`, `POST /v1/projects/{project_id}/allocate`, `POST /v1/projects/{project_id}/release`)
- Test: `tests/test_auth_projects.py`

**Interfaces:**
- Consumes: `onboarding.licence_valid`, `authz.assert_facility`.
- Produces: `projects.create(conn, *, actor: User, customer_name: str, contract_ref: str, facility_id: str, starts_on: date, ends_on: date | None, clock) -> str`; `projects.allocate(conn, *, actor: User, project_id: str, driver_id: str, clock) -> None` (requires APPROVED, valid licence, same facility, project `ACTIVE`, creator or ADMIN); `projects.release(...)`; `projects.drivers_of(conn, project_id: str) -> list[str]`; `projects.visible_driver_ids(conn, user: User) -> set[str]` (drivers on projects the warehouse authority created; ADMIN all).

- [ ] **Step 1: Write failing tests:** `test_warehouse_creates_project_in_own_facility_only`; `test_allocate_requires_approved_and_valid_licence`; `test_cannot_allocate_across_facilities`; `test_other_warehouse_cannot_allocate_to_my_project`; `test_driver_can_be_on_two_projects`; `test_release_sets_released_at`; `test_duplicate_contract_ref_refused`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** per signatures.
- [ ] **Step 4: Run** `pytest tests/test_auth_projects.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: customer-contract projects and driver allocation`.

### Task 10: Guard existing surfaces

**Files:**
- Modify: `src/setuhaul/api.py` (coordinator case routes accept a session with role `WAREHOUSE_AUTHORITY` or `ADMIN`, scoped by facility, in addition to the legacy bearer token), `src/setuhaul/demo_ui.py` (every `/demo` and `/demo/api/*` route requires a web sign-in as `WAREHOUSE_AUTHORITY` or `ADMIN`; the demo's driver chat panel keeps working for those roles as a harness), partner-email and driver-chat read routes
- Test: `tests/test_auth_role_matrix.py`; update `tests/test_api.py`, `tests/test_demo_ui.py` fixtures to sign in

**Interfaces:**
- Consumes: `authz.require_role`, `authz.facility_ids`, `projects.visible_driver_ids`.

- [ ] **Step 1: Write the role matrix test:** parametrize every route in `app.routes` (not a hand-written list) against roles `ADMIN`, `WAREHOUSE_AUTHORITY` and anonymous, asserting the expected allow/deny from the spec (the Telegram webhook and HMAC channel routes are listed as exempt, each with its own header check); `test_no_route_serves_board_data_without_web_role` (including a request carrying a Telegram secret header); `test_warehouse_sees_chats_only_for_project_drivers`; `test_warehouse_cannot_read_other_facility_cases`.
- [ ] **Step 2: Run** `pytest tests/test_auth_role_matrix.py -v`. Expected: FAIL.
- [ ] **Step 3: Apply guards** to each route; keep `X-Signature` phone-channel routes and `/health`, `/ready` unchanged.
- [ ] **Step 4: Run** `pytest tests -q`. Expected: all PASS (fixtures updated).
- [ ] **Step 5: Commit** `feat: enforce roles on cases, board, chats and demo console`.

### Task 11: Role screens

**Files:**
- Create: `src/setuhaul/auth/pages.py`, `src/setuhaul/auth/templates/` (login, admin, warehouse pages)
- Modify: `pyproject.toml` (`package-data` for the templates), `src/setuhaul/auth/routes.py`
- Test: `tests/test_auth_pages.py`

**Interfaces:**
- Produces: `GET /` redirects by role (`/admin`, `/warehouse`) or to `/auth/login`; `/admin` (users and invites, pending drivers, audit log with filters actor/action/outcome/date); `/warehouse` (dock board; messages: cases, partner emails, project drivers' chats; projects; add driver; allocate; each driver's Telegram status with a revoke button). Drivers have no web page.

- [ ] **Step 1: Write failing tests:** `test_root_redirects_by_role`; `test_admin_page_shows_pending_driver_and_audit_rows`; `test_warehouse_page_has_board_and_no_audit_link`; `test_warehouse_can_revoke_telegram_binding_and_it_is_audited`; `test_pages_escape_user_text` (a driver name of `<script>` renders escaped).
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** pages by reusing the existing board and chat API routes from the browser; templates are plain HTML with escaped values.
- [ ] **Step 4: Run** `pytest tests/test_auth_pages.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: admin and warehouse screens`.

### Task 12: Documentation, env and full verification

**Files:**
- Modify: `.env.example` (new variables with comments), `README.md`, `docs/running-and-demonstrating.md` (Google client setup, BotFather bot creation and webhook registration, bootstrap admin, break-glass), `docs/decision-register.md`
- Test: whole suite

- [ ] **Step 1: Update docs** including the owner's Google Cloud steps (create web OAuth client, redirect URI `<base>/auth/callback`; create the Telegram bot with BotFather and call `setWebhook` with the secret token; set the variables).
- [ ] **Step 2: Run** `ruff check . && mypy src && python scripts/check_comment_only.py && pytest -q`. Expected: no errors, all PASS.
- [ ] **Step 3: Manual smoke** against `scripts/run_demo.py` with the fake verifier switch: sign in as bootstrap admin, invite a warehouse authority, add and approve a driver, create a project, allocate, then post fixture Telegram updates (share contact, then a message) and confirm the driver is bound and answered and that nothing from the bot returns board data. Record the result in `docs/verification/auth-roles-run.md`.
- [ ] **Step 4: Commit** `docs: auth and roles setup, verification run`.
