# Authentication, roles and driver onboarding: design

Status: draft for approval (2026-11-24). Size: project (new identity layer, four roles, onboarding, projects, audit).

## Intent and why
Today anyone who can reach the service can open the demo console, the dock board and the coordinator API.
Drivers are identified only by a signed phone channel, coordinators by static bearer tokens in an environment variable,
and there is no record of who did what. The business needs real sign-in, role-based screens, driver onboarding with
admin approval, allocation of drivers to customer contracts, and an audit trail an admin can review.

## Goals
- **Google sign-in only** for web users (admins and warehouse authorities). No local user or password maintenance.
- **Drivers do not sign in to the web at all.** Their identity is their Telegram account (Telegram authenticates them),
  bound once to the driver record a warehouse authority registered. Drivers talk to the service only through a Telegram bot.
- **One exception:** an optional local admin password, a break-glass login for when Google is unavailable.
- Two web roles with server-enforced access (a hidden menu is not security), plus drivers on Telegram:
  - `ADMIN`: invites warehouse authorities and admins, approves drivers, reviews the audit log, sees everything.
  - `WAREHOUSE_AUTHORITY`: bound to one or more facilities. Sees the dock board, escalation cases and partner emails for
    those facilities, and driver chats only for drivers allocated to projects they created. Creates projects, adds drivers,
    allocates approved drivers to projects.
  - **Driver (Telegram only, not a web user):** the bot shows only that driver's own loads, appointments and chat. There is no
    way for a driver to reach the dock board.
- **Invite-only access.** A Google account without an invite is refused even though Google sign-in succeeded.
- **Driver onboarding:** a warehouse authority adds a driver (name, phone, licence number, licence expiry, vehicle,
  carrier). The driver stays `PENDING_APPROVAL` until an admin approves. Only `APPROVED` drivers can be allocated or use the bot.
- **Telegram binding by shared contact:** the driver opens the bot and taps "Share my phone number". Telegram supplies the
  number and the sender's own account ID. The bot accepts it only when the contact's `user_id` equals the sender's account ID
  (so a forwarded or someone else's contact is refused), normalises the number to E.164, and matches it to the registered,
  `APPROVED` driver. A match creates the binding (`telegram_identities`); anything else gets a neutral refusal and an audit row.
- **Telegram channel:** a webhook route verifies Telegram's secret-token header, resolves the sender's account ID to a bound
  driver, and passes the message into the same conversation engine as the existing channel. The engine is unchanged.
- **Transport project = customer contract.** Created and managed by warehouse authorities; drivers are allocated to it.
- **Audit log** (append-only) of every sign-in (including refused ones), invite, approval, allocation and break-glass use.

## Non-goals
- Other identity providers, or any local password except the admin backup.
- Any driver web login, driver page, or driver email. The existing phone channel (HMAC-signed) is kept only as the engine's
  test and demo harness, not a second production path.
- Self-registration, password reset flows, MFA management (Google's own MFA applies).
- Billing, contract pricing or customer portals. A project is a reference record plus allocations.
- Moving data between environments, SSO group sync.

## Anti-goals
- A refused or unknown Google account must never create a user row or a session.
- A driver must never receive dock board data, other drivers' data, or other facilities' data, by any route (the bot included).
- An unbound, unapproved, rejected or licence-expired Telegram account must never reach the conversation engine.
- The break-glass password must never be a quiet default: it is off unless configured, rate-limited, and audited.
- No role is decided by the browser; every route checks the session's role on the server.
- Never edit the supplied driver, vehicle or carrier rows (see data contract change).

## Constraints
- Stack: FastAPI, SQLite migrations in `migrations/`, no new heavy services. Reuse the `sha256(token)` session pattern from
  `driver_sessions`. Python 3.12.
- The Telegram bot is created by the owner with BotFather; its token and a webhook secret are supplied through environment
  variables. The webhook URL must be HTTPS and reachable by Telegram.
- Google OAuth client ID and secret are created by the owner in Google Cloud and supplied through environment variables.
  The service never stores them in the repo. Redirect URI must be configured by the owner.
- `docs/writable-data-contract.md` currently marks `drivers`, `vehicles`, `carriers` read-only. See Data contract change.

## Approaches considered
1. **Server-side Google login (authorization-code flow with PKCE), HttpOnly session cookie (chosen).** Roles enforced
   per route. Reuses the hashed-session pattern. Needs an owner-created OAuth client.
2. Google button in the page posting an ID token. Less redirect handling, but ties auth to page scripts and needs separate
   wiring for the demo console and real screens. Rejected.
3. Outside identity service (Auth0, Keycloak). Least code but adds a service to run, against "no local maintenance". Rejected.

For driver identity the user chose: Telegram is the driver's authentication; no web sign-in for drivers; binding by
"share contact" (the invite-link alternative was rejected: needs someone to deliver the link, and a leaked link can be claimed).
For onboarding data the user chose: amend the data contract for insert-only writes (see below).

## Design

### Data contract change (decided)
Allow **insert-only** writes to `drivers`, `vehicles` and `carriers`; supplied rows are never updated or deleted.
New drivers are real `drivers` rows, so scheduling, ETA, holds and the phone channel work unchanged.
Document in `docs/writable-data-contract.md` and `docs/decision-register.md`.
Because `drivers.driver_status` defaults ACTIVE, new drivers are inserted as `INACTIVE` and set `ACTIVE` only at approval
(an update of a row this feature inserted, which is allowed; supplied rows stay untouched, checked by `created_by_user_id`
in `driver_profiles`).

### Migration `011_auth_roles.sql` (extension tables)
- `users(user_id, email UNIQUE COLLATE NOCASE, role ADMIN|WAREHOUSE_AUTHORITY, status ACTIVE|DISABLED, display_name, invited_by, created_at, last_login_at)`.
  Rows exist only via invite. Drivers are never users.
- `user_facilities(user_id, facility_id)`: facilities a warehouse authority manages.
- `auth_sessions(session_id = sha256(token), user_id, created_at, expires_at, revoked_at, auth_method GOOGLE|LOCAL_ADMIN)`.
  Cookie is HttpOnly, Secure outside localhost, SameSite=Lax; 8-hour life.
- `admin_credentials(user_id, password_hash, salt, failed_attempts, locked_until)`: scrypt (stdlib). Present only if an
  admin sets one; the feature is off if `SETUHAUL_LOCAL_ADMIN` is not enabled.
- `driver_profiles(driver_id PK, licence_expiry, vehicle_id, approval_status PENDING_APPROVAL|APPROVED|REJECTED,
  approved_by, approved_at, created_by_user_id)`.
- `telegram_identities(telegram_user_id PK, chat_id, driver_id UNIQUE, bound_at, revoked_at)`: one account per driver.
- `projects(project_id, customer_name, contract_ref UNIQUE, facility_id, status ACTIVE|CLOSED, starts_on, ends_on, created_by)`.
- `project_drivers(project_id, driver_id, allocated_by, allocated_at, released_at)`; a driver may be on several projects.
- `audit_log(audit_id, ts, actor_user_id, actor_email, action, target_type, target_id, outcome, ip, detail_json)`.
  Append-only, enforced by triggers that reject UPDATE and DELETE.

### Sign-in flow
1. `GET /auth/login` redirects to Google (state and PKCE verifier stored in a short-lived signed cookie).
2. `GET /auth/callback` exchanges the code, verifies the ID token (issuer, audience, expiry, `email_verified`), looks the
   email up in `users`. Not found or DISABLED: refuse, write an audit row, create nothing.
3. Success: create `auth_sessions` row, set cookie, audit `LOGIN_OK`. `POST /auth/logout` revokes the session.
4. Break-glass: `POST /auth/local-admin` (admin email + password). Allowed only when enabled; 5 failures lock the account
   for 15 minutes; every attempt is audited; sessions from it last 1 hour.

### Authorization
- One dependency, `require_role(*roles)`, resolves the cookie to a user and rejects otherwise (401 unauthenticated,
  403 wrong role). Facility and project scoping are applied in the query, not after fetching.
- Route map:
  - Dock board, partner emails, escalation cases: `WAREHOUSE_AUTHORITY` (own facilities) and `ADMIN`.
  - Driver chats: warehouse authority limited to drivers on projects they created; admin all.
  - Projects and allocations: warehouse authority (own facility), admin read.
  - Driver approval, invites, audit log: `ADMIN`.
  - Driver chat: Telegram webhook only; the `driver_id` comes from the bound Telegram account, never from message content or
    request parameters.
- The existing phone-channel routes keep their HMAC check (test and demo harness only); coordinator bearer tokens remain only as a service-to-service
  fallback until the owner removes them. The `/demo` console moves behind sign-in (role views match the table above).

### Screens (server-rendered, role-specific)
- Admin: users and invites, pending driver approvals, audit log with filters (actor, action, date, outcome).
- Warehouse authority: dock board, messages (cases, partner emails, project drivers' chats), projects, add driver, allocate,
  and each driver's Telegram status (not bound / bound / revoked), with a button to revoke the binding.
- Driver: the Telegram bot only (loads, appointments, rescheduling chat). No web screen.

### Rules worth stating
- Licence expiry in the past: the driver cannot be allocated and the bot refuses them; shown to the warehouse authority as a flag.
- Rejecting or suspending a driver, or revoking the binding, stops bot access immediately. A driver can re-bind only while
  `APPROVED` with a valid licence.
- Allocation requires `APPROVED`, an unexpired licence, and a project in the same facility as the warehouse authority.
- Disabling a user revokes all their sessions immediately.
- Audit rows never contain passwords, tokens, or one-time codes.

## Testing (to be planned in detail)
Role matrix test for every route (each web role, allowed and forbidden, and anonymous); invite-only refusal; unverified-email refusal; expired
state/PKCE; session expiry and revocation; scrypt lockout; audit append-only triggers; driver cannot read the board by any
route; licence-expiry blocks. Telegram: wrong or missing secret-token header refused; contact whose `user_id` is not the
sender refused; unregistered, unapproved or expired driver refused; number normalisation (`+91 90000 10006`, `09000010006`,
`919000010006`); second account for the same driver refused; revoked binding refused. Google is faked at the token-verification
boundary and Telegram updates are posted as fixture JSON.

## Left to the builder
Exact URL paths and page markup, scrypt parameters, cookie names, CSS/layout, how the fake Google provider is wired in tests,
and the order of work (suggest: migration and audit, sessions and Google flow, route guards, then screens).

## Owner actions (the builder cannot do these)
Create a Google Cloud OAuth client (web application), set the redirect URI, create the Telegram bot with BotFather and
register its webhook to the service's HTTPS URL, and provide `SETUHAUL_GOOGLE_CLIENT_ID`,
`SETUHAUL_GOOGLE_CLIENT_SECRET`, `SETUHAUL_SESSION_SECRET`, `SETUHAUL_TELEGRAM_BOT_TOKEN`, `SETUHAUL_TELEGRAM_WEBHOOK_SECRET`, the first admin's email (`SETUHAUL_BOOTSTRAP_ADMIN_EMAIL`),
and optionally `SETUHAUL_LOCAL_ADMIN=1` with a password set through a one-time CLI command.

## Calls made by the assistant (not yet confirmed by you)
- Warehouse authorities scoped to facilities through `user_facilities`.
- New drivers inserted `INACTIVE` until approval; approval flips to `ACTIVE`.
- 8-hour sessions, 1-hour break-glass sessions; 5-failure, 15-minute lockout.
- Bot replies to Telegram go out through the Bot API using the bot token; outbound messages the service already queues for
  drivers (notification outbox, recipient `DRIVER`) are delivered to the bound chat.
- Bot refusals are deliberately neutral ("Please contact your warehouse") and never reveal whether a phone is registered.
- Bootstrap: the first admin comes from an environment variable, since invites need an admin to exist.
- Coordinator bearer tokens stay as a service-to-service fallback for now.
