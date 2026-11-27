# Email invitations and password sign-in

Date: 2026-11-27. Status: approved.

## Intent

An admin invites someone, the app emails them a link, and they choose a password that nobody else can see. If they forget
it, they ask for a reset link by email. This lets people who will not or cannot use Google sign in.

## Goals

- Inviting a user sends them an email with a link to set a password.
- The user can sign in with email and password, or with Google as today.
- "Forgot password?" on the sign-in page emails a reset link.
- Nobody, including an admin, can see a password. Only a scrypt hash is stored.

## Non-goals

- Self-registration. Access stays by invitation.
- Two-factor sign-in, password history, or forced rotation.
- A "change password" screen for signed-in users (a reset link does this).
- Email settings on the Admin Configuration screen. Email is set up in `.env`.
- The demo server (`demo_ui.py`) and the Telegram driver side.

## Anti-goals

- Anyone learning whether an email address has an account.
- Turning this on also sending real partner warehouse requests.
- A link, token or password appearing in logs or the audit log.

## Approaches

| Approach | Trade-off | Result |
|---|---|---|
| Emailed one-time links | Needs a sending mailbox on the server | **Picked**: it is the self-service flow asked for |
| Admin copies a one-time link | No mailbox, but no self-service reset | Not picked |
| Outside sign-in service | Handles email and passwords, but a large change and a new dependency | Not picked |

## Switching it on

The feature is on only when all of these are set:

- `SETUHAUL_PASSWORD_LOGIN=1`
- `SETUHAUL_PUBLIC_URL` (links are built from this only, never from the request's Host header)
- `SETUHAUL_SMTP_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_FROM` (same names and defaults as today)

IMAP settings and `SETUHAUL_PARTNER_EMAIL_MODE` are neither needed nor changed. If the switch is on but something is
missing, the service logs which variable (never its value) and starts with the feature off.

With the feature off, the app behaves exactly as today, including the admin-only backup password
(`SETUHAUL_LOCAL_ADMIN=1`). No new libraries are needed.

## What people see

**Admin, Users tab**

- "Invite" creates the user as today, then emails the link: "Invitation emailed."
- If sending fails, the user still exists: "Invited, but the email could not be sent."
- Each active user has "Send password link", which covers resending an invitation and helping someone who is stuck.
- The table shows whether a password has been set.

**Sign-in page**

    [ Sign in with Google ]
    Email     [______________]
    Password  [______________] Show
    [ Sign in ]   Forgot password?

This replaces the folded "Administrator password" form when the feature is on.

**Forgot password**: one email box. The reply is always "If that address has access, we have sent it a link."

**Set password**: opened from the emailed link. New password, repeated, with the 12-character minimum shown. On success
it returns to the sign-in page. A used, expired or unknown link says so and points to "Forgot password?".

## Rules

**Links**

- A random 32-byte token, stored only as its sha256, usable once.
- An invitation link lasts 48 hours; a reset link lasts 1 hour.
- A new link cancels the user's earlier unused ones.
- The token goes after `#` in the address, so it is never sent in a request line or written to server or Caddy logs. The
  page's script posts it in the request body.
- Using a link is atomic: two submissions cannot both succeed.

**Passwords**

- 12 to 1024 characters, hashed with the existing scrypt code. The existing `admin_credentials` table holds them for
  every role.
- 5 wrong attempts lock password sign-in for that account for 15 minutes. Google sign-in is unaffected.
- A locked, unknown, disabled or password-less account all get the same "not correct" reply.
- Setting a password clears the lock and ends all of that user's sessions.
- Asking for a link changes nothing about the current password.

**Who**

- Every role, admins included. An admin account is then as safe as that admin's mailbox.
- Only active users get links. Disabling a user cancels their unused links.
- Any active user may use "Forgot password?", including one who has only used Google, to set a first password.

**Sessions**: a password sign-in lasts 8 hours, like Google, and is recorded as `PASSWORD`.

**Limits on sending**

- Per account: one email per 2 minutes and 5 per hour.
- All "Forgot password?" requests together: 30 emails per hour.
- Counted in Redis. If Redis is down, "Forgot password?" sends nothing and says to try again later.
- Limits are not per IP address: behind Caddy the app currently sees the proxy's address.
- "Forgot password?" sends after replying, so response time does not reveal whether the account exists.

**Audit**: link sent, send failed, link requested, password set, and every password sign-in attempt. Never the token,
link or password.

## Data

Migration `020` adds a table for links (token hash, user, purpose, created, expires, used) and lets `auth_sessions`
record `PASSWORD`. SQLite needs the sessions table rebuilt to widen that check; existing sessions are kept.

## Tests

Written first, using the existing in-memory mailbox, so no test sends real mail. They cover: invite sends one link;
the link works once and expires; the same reply for known and unknown addresses; disabled users; lockout; sessions
ended on password change; sending limits; nothing secret in the audit log; feature off behaves as today; and the
migration on a database that already has sessions.

## Left to the builder

Route and module names, page markup within the existing style and security headers, the exact email wording, and how the
sessions table is rebuilt.

## Before it works on the live site

- A sending mailbox: any Gmail account with 2-Step Verification and an app password, entered in the server's `.env` by
  the owner. A dedicated one is safer, because an app password also opens the mailbox. The Google sign-in client id and
  secret cannot send mail: they only identify who is signing in.
- A check that the server can reach the mail server's port. Not verified.
- `docs/deployment-hostinger.md` and `.env.example` updated with the new switch.
