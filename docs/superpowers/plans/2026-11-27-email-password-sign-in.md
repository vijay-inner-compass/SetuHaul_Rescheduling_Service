# Email invitations and password sign-in: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An invited user gets an emailed link, sets a password nobody can see, signs in with it, and can reset it.

**Architecture:** One new table of hashed one-time links; the existing scrypt/lockout code in `local_admin.py` serves
every role; the existing `SmtpSender` sends the mail. Everything is off unless `deps.password_login_on`.

**Tech Stack:** Python, FastAPI, SQLite, Redis, server-rendered pages with external JS, pytest. No new libraries.

**Spec:** `docs/superpowers/specs/2026-11-27-email-password-sign-in-design.md`

## Before starting

Wiki work was uncommitted on `feat/wiki` in the main checkout when this was written, touching `auth/pages.py`,
`auth/assets/app.css` and `tests/test_auth_role_matrix.py`, which this plan also changes. Work in a separate git
worktree. Base it on the branch that holds the finished wiki work (normally `main` after it is merged); ask the owner if it
is not merged. Never switch branches or commit in the main checkout. Locate code by function name, not line number.

This plan and its spec were untracked in the main checkout: copy both into the worktree and commit them there first.

## Global Constraints

- Feature on only with `SETUHAUL_PASSWORD_LOGIN=1`, `SETUHAUL_PUBLIC_URL`, `SETUHAUL_SMTP_USER`, `SETUHAUL_SMTP_PASSWORD`
  (host, port and from keep today's defaults). IMAP and `SETUHAUL_PARTNER_EMAIL_MODE` are neither needed nor changed.
- Feature off: behaviour exactly as today, including `SETUHAUL_LOCAL_ADMIN=1`; the new routes answer 404 `NOT_FOUND`.
- Links: 32 random bytes, stored only as sha256, single use; `INVITE` 48 h, `RESET` 1 h; a new link cancels earlier ones.
- Links are `<public_url>/set-password#token=<token>`, never built from the Host header.
- Passwords: 12 to 1024 characters. 5 failures lock for 15 minutes. Password sessions last 8 hours, method `PASSWORD`.
- Sending: per account 1 per 2 minutes and 5 per hour; all forgot-password emails 30 per hour.
- Forgot-password reply, always: `If that address has access, we have sent it a link.`
- No token, link or password in logs, audit rows or responses.
- Pages use `SECURITY_HEADERS`, `esc()` and external scripts only.
- Before each commit: `.\.venv\Scripts\python -m pytest -q`, `-m ruff check .`, `-m mypy src` all pass.

## Review Focus

- A mail scanner opens the link: a GET must not use it up (Task 3).
- A too-short password on the set page: the link must still work afterwards (Task 3).
- A missing or non-string field, or a non-JSON body, on a public route: a clean refusal, never a 500 (Tasks 2, 3).
- The address typed with capitals or spaces: finds the same account (Tasks 2, 4).
- An admin opens an invite link in their own signed-in browser: their session is untouched (Task 3).

## Files

| File | Responsibility |
|---|---|
| `migrations/020_password_sign_in.sql` | links table; `auth_sessions` accepts `PASSWORD` |
| `src/setuhaul/auth/links.py` (new) | issue, redeem, cancel, send limits, send |
| `src/setuhaul/auth/account_mail.py` (new) | settings from the environment; email wording |
| `src/setuhaul/auth/password_routes.py` (new) | the four JSON routes |
| `src/setuhaul/auth/password_pages.py` (new) | forgot-password and set-password pages |
| `src/setuhaul/auth/assets/password.js` (new) | reads the token from `#`; checks the repeat field |
| `local_admin.py`, `sessions.py`, `users.py`, `admin_api.py`, `routes.py`, `pages.py`, `signin_notice.py`, `common.js`, `api.py`, `mailcfg.py` | small edits |
| `tests/test_password_sign_in.py` (new) | all new tests |

---

### Task 1: Migration and the `PASSWORD` session

**Files:** create `migrations/020_password_sign_in.sql`; modify `auth/sessions.py`; test `tests/test_password_sign_in.py`.

**Produces:** table `account_links(token_hash TEXT PK, user_id TEXT NOT NULL REFERENCES users, purpose TEXT CHECK IN
('INVITE','RESET'), created_at TEXT, expires_at TEXT, used_at TEXT)` with an index on `user_id`;
`sessions.create(conn, user, "PASSWORD", clock)`.

- [ ] **Step 1: Failing tests.**
  - `test_migration_keeps_existing_sessions`: copy the seed database; apply the migrations below `020` from a temporary
    directory; insert a user and a `GOOGLE` session; run `apply_migrations(conn)`. Assert it returns
    `["020_password_sign_in.sql"]`, the session row is unchanged, and `ix_auth_sessions_user` exists.
  - `test_password_session_lasts_8_hours`: `sessions.resolve` returns the user at 7 h 59 min and `None` at 8 h 1 min.
- [ ] **Step 2:** `pytest tests/test_password_sign_in.py -q`. Expected: FAIL (no migration; CHECK constraint).
- [ ] **Step 3:** Write the migration: create `account_links`; rebuild `auth_sessions` (create new, copy, drop, rename,
  recreate the index) with the check `('GOOGLE','LOCAL_ADMIN','PASSWORD')`. `apply_migrations` already turns foreign
  keys off and runs it in one transaction. Add `"PASSWORD": timedelta(hours=8)` to `sessions._TTL` and the `Literal`.
- [ ] **Step 4:** Tests pass. Update any test that counts or lists migrations.
- [ ] **Step 5:** Commit `feat: add account links and password sessions`.

### Task 2: Password sign-in for any role

**Files:** modify `auth/local_admin.py`, `api.py` (`AppDeps`); create `auth/password_routes.py`; register it in `api.py`
beside `register_auth_routes`; add the route to `PUBLIC` in `tests/test_auth_role_matrix.py`.

**Produces:**
- `AppDeps.password_sender: MailSender | None = None`; property `password_login_on -> bool` (sender set and
  `public_url` non-empty). Routes read it per request.
- `local_admin.hash_new_password(password: str) -> tuple[str, str]` (stored, salt hex; raises the length codes)
- `local_admin.store_password(conn, user_id: str, stored: str, salt_hex: str) -> None` (no transaction of its own;
  clears the lock). `set_password` keeps its signature and uses both.
- `local_admin.verify(conn, email, password, clock, ip=None, *, admin_only: bool = True) -> User`. With
  `admin_only=False` any active role may pass, and audit rows use action `PASSWORD_LOGIN` with outcomes `PASSWORD_OK`,
  `PASSWORD_REFUSED`, `PASSWORD_LOCKED`. Defaults leave today's rows unchanged.
- `register_password_routes(app: FastAPI, deps: AppDeps) -> None`: `POST /auth/password` `{email, password}`.

- [ ] **Step 1: Failing tests.** Fixture `world`: `build_world`, then `deps.password_sender = InMemoryMailbox()` and
  `deps.public_url = "http://localhost"`.
  - `test_warehouse_user_signs_in`: 200; cookie max-age 28800; `/auth/me` shows the role; session method `PASSWORD`.
  - `test_same_refusal_for_unknown_disabled_passwordless_and_locked`: each is 401 `BAD_CREDENTIALS`.
  - `test_email_is_case_and_space_insensitive`: `"  WHA@X.com "` signs in.
  - `test_five_failures_lock_15_minutes`
  - `test_off_is_404`: with `password_sender = None`.
  - `test_bad_bodies_are_refused`: non-JSON, `{}`, a 1025-character password: 401 `BAD_CREDENTIALS`.
- [ ] **Step 2:** Run. Expected: FAIL (404 / unknown argument).
- [ ] **Step 3:** Implement. Follow `local_admin_login` in `routes.py` (threadpool, audited bad body, `cookie_kwargs`).
- [ ] **Step 4:** New tests and `tests/test_auth_local_admin.py` pass.
- [ ] **Step 5:** Commit `feat: password sign-in for invited users`.

### Task 3: One-time links and setting a password

**Files:** create `auth/links.py`; modify `auth/password_routes.py`, `auth/users.py` (`disable`), role-matrix `PUBLIC`.

**Produces:**
- `links.issue(conn, user_id: str, purpose: Literal["INVITE","RESET"], clock) -> str` (raw token; marks the user's
  earlier unused links used)
- `links.redeem(conn, token: str, password: str, clock, ip: str | None = None) -> User`; raises `AuthError`
  `LINK_INVALID`, `PASSWORD_TOO_SHORT`, `PASSWORD_TOO_LONG`
- `links.cancel_all(conn, user_id: str, clock) -> None` (inside the caller's transaction)
- `POST /auth/password/set` `{token, password}`: 200, or 422 with the code

Order inside `redeem`: hash the password first (this validates length and keeps scrypt outside the write lock), then in
one `immediate(conn)`: select an unused, unexpired link whose user is active; mark it used; `store_password`; revoke all
that user's sessions; audit `PASSWORD_SET` / `OK`. Unknown, used, expired and disabled all raise `LINK_INVALID`.

- [ ] **Step 1: Failing tests.**
  - `test_link_sets_password_once`: set succeeds, sign-in works, second use is `LINK_INVALID`.
  - `test_only_the_hash_is_stored`: the raw token appears in no table.
  - `test_expiry`: `INVITE` works at 47 h, fails at 49 h; `RESET` fails at 61 min.
  - `test_new_link_cancels_old`, `test_disable_cancels_links` (still invalid after re-enable)
  - `test_short_password_keeps_link_usable`
  - `test_setting_password_ends_sessions_and_clears_lock`
  - `test_two_simultaneous_redeems_one_wins` (two threads, own connections)
  - `test_get_does_not_consume_and_bad_bodies_are_422`
  - `test_other_signed_in_user_is_untouched`: an admin client posts another user's token; the admin stays signed in and
    no session cookie is set.
  - `test_no_secret_in_audit`: neither token nor password appears in any `audit_log` row.
- [ ] **Step 2:** Run. Expected: FAIL (`links` missing).
- [ ] **Step 3:** Implement. Token and hash as in `sessions.py`.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat: one-time password links`.

### Task 4: Sending the links

**Files:** create `auth/account_mail.py`; modify `links.py`, `password_routes.py`, `admin_api.py`; role matrix
(`/auth/password/forgot` in `PUBLIC`, `/v1/admin/users/{user_id}/password-link` as `ADMIN`).

**Consumes:** `OutgoingMail`, `MailSender`, `MailDeliveryError` from `mail_transport`.

**Produces:**
- `account_mail.link_mail(to_addr: str, purpose: str, url: str) -> OutgoingMail`. Subjects: `You're invited to SetuHaul`
  and `Reset your SetuHaul password`. The body gives the link, its lifetime, and says to ignore it if unexpected.
  Subjects contain "SetuHaul", which the partner reply reader searches for: if both use one mailbox, check these are
  ignored and add a test for it.
- `links.send(deps: AppDeps, user_id: str, purpose: str, *, forgot: bool = False) -> Literal["SENT","FAILED","THROTTLED"]`:
  limits, issue, send, audit `ACCOUNT_LINK` with `SENT`, `SEND_FAILED` or `THROTTLED`. Redis keys `pwmail:*`. If Redis
  fails and `forgot` is false (an admin is sending), send anyway.
- `POST /auth/password/forgot` `{email}`: ping Redis (503 `TRY_LATER` if down); audit `ACCOUNT_LINK` / `REQUESTED`; reply;
  then look up and send in a Starlette `BackgroundTask`, purpose `RESET`, active users only.
- `POST /v1/admin/users` response gains `invite_email: "SENT" | "FAILED" | null` (null when the feature is off).
- `POST /v1/admin/users/{user_id}/password-link`: `INVITE` if no password yet, else `RESET`. 200; 404 `UNKNOWN_USER`;
  409 `USER_DISABLED`; 429 `TOO_SOON`; 502 `EMAIL_FAILED`.

- [ ] **Step 1: Failing tests** (all with `InMemoryMailbox`).
  - `test_invite_sends_one_link_that_works`: one mail to the invitee; its token redeems.
  - `test_invite_survives_send_failure`: sender raises `MailDeliveryError`; 201, `invite_email == "FAILED"`, user exists.
  - `test_invite_with_feature_off_sends_nothing`
  - `test_forgot_same_reply_known_and_unknown`: identical status and body apart from `request_id`; mail only for known.
  - `test_forgot_normalises_email`, `test_forgot_disabled_user_gets_nothing`
  - `test_limits`: second within 2 minutes not sent; sixth within an hour not sent; 31st forgot email overall not sent.
  - `test_forgot_redis_down_sends_nothing`
  - `test_link_uses_public_url_not_host_header`
  - `test_admin_send_link_codes`
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. Fakeredis expiry does not follow `FakeClock`: delete keys in the tests to move time on.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat: email invitation and reset links`.

### Task 5: Screens

**Files:** create `auth/password_pages.py`, `auth/assets/password.js`; modify `pages.py` (`_landing`, `_users_section`,
`ASSET_TYPES`, `register_pages`), `common.js`, `signin_notice.py`, role matrix (`GET /forgot-password`,
`GET /set-password` in `PUBLIC`).

- Sign-in page, feature on: email, password with Show, `Sign in` (posts `/auth/password`, `data-next="/"`) and a
  `Forgot password?` link, in place of the folded administrator form; Google button unchanged; lead text no longer says
  "Administrator access."
- `/set-password`: hidden `token`, `password`, and a repeat field with **no** `name` (so it is not sent). `password.js`
  fills the token from `location.hash`, removes it with `history.replaceState`, and uses `setCustomValidity` for a
  mismatch. `data-next="/?signin=password_set"`; notice: `Password saved. Sign in below.`
- `common.js` messages: `LINK_INVALID` ("This link has been used or has expired. Use Forgot password? to get a new
  one."), `TOO_SOON`, `TRY_LATER`, `EMAIL_FAILED`, `USER_DISABLED`. After invite, flash `Invitation emailed.` or
  `Invited, but the email could not be sent.` from `invite_email`.
- Users table, feature on: a `Password` column (`Set` / `Not set`) and a `Send password link` button for active users.

- [ ] **Step 1: Failing tests:** `test_login_page_on_and_off` (off: today's markup, no `/auth/password`),
  `test_pages_404_when_off`, `test_pages_have_security_headers_and_no_inline_script`, `test_users_table_columns`,
  `test_password_set_notice`. Add `password.js` to `ASSETS` in `tests/test_auth_pages.py`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement, reusing `_layout`, `_button` and the sign-in page styles.
- [ ] **Step 4:** Run the whole suite. Expected: PASS.
- [ ] **Step 5:** In a browser against a local run with `InMemoryMailbox`, walk invite, set, sign in, forgot, reset, at
  desktop and phone width. No console or CSP errors.
- [ ] **Step 6:** Commit `feat: password screens`.

### Task 6: Start-up settings and documentation

**Files:** modify `mailcfg.py`, `account_mail.py`, `api.py` (`create_app_from_env`), `.env.example`,
`docs/deployment-hostinger.md`, `docs/running-and-demonstrating.md`.

**Produces:**
- `mailcfg.load_smtp_settings() -> MailSettings`: reads `.env` and the environment without the live partner-mail checks;
  `load_mail_settings` keeps its behaviour.
- `account_mail.account_sender_from_env(env: Mapping[str, str]) -> tuple[MailSender | None, tuple[str, ...]]`: sender, or
  `None` with the names of missing variables (empty names when the switch is off).

- [ ] **Step 1: Failing tests:** `test_switch_off_gives_no_sender`, `test_missing_settings_are_named_not_valued`,
  `test_complete_settings_give_smtp_sender`, `test_partner_mode_unchanged` (still `simulated`).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement; `create_app_from_env` loads `.env` before reading, sets `password_sender`, and logs a warning
  naming missing variables.
- [ ] **Step 4:** Docs: the four settings, the Gmail app password steps, and that the SMTP port must be reachable from
  the server. Check how `deploy/` applies migrations and say what the owner must do for `020`.
- [ ] **Step 5:** Full suite, ruff, mypy pass. Commit `docs: password sign-in set-up`.

## Not covered by tests

Real delivery through Gmail and the server's access to the mail port. The owner checks both after deploying, by inviting
a second address of their own.
