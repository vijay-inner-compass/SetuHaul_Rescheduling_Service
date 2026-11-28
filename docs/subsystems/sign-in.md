---
title: Web sign-in and password links
type: subsystem
created: 2026-11-27
updated: 2026-11-28
sources:
  - code: src/setuhaul/auth/sessions.py
  - code: src/setuhaul/auth/links.py
  - code: src/setuhaul/auth/password_routes.py
  - code: src/setuhaul/auth/local_admin.py
  - code: src/setuhaul/auth/account_mail.py
  - code: src/setuhaul/api.py
  - code: src/setuhaul/auth/settings_routes.py
verified_at: b5b462f
---

# Web sign-in and password links

Who can get a web session. Access is by invitation whichever way they sign in; drivers have no web account. Settings
and owner set-up steps are in
[running and demonstrating](../running-and-demonstrating.md#7a-sign-in-roles-and-the-telegram-bot).

## Three ways in

| Method | On when | Session |
|---|---|---|
| Google | client id, secret and a long enough session secret | 8 hours |
| Email and password | public URL and a usable mail sender (`src/setuhaul/api.py:257`) | 8 hours |
| Break-glass admin password | `SETUHAUL_LOCAL_ADMIN=1` | 1 hour |

The mail sender needs `SETUHAUL_PASSWORD_LOGIN` to be `1`; that switch and the mailbox, when saved on the Configuration
tab, win over the environment (`src/setuhaul/auth/account_mail.py:19`, `src/setuhaul/api.py:235`). The site address stays
in `.env`. Sessions are server-side, only the token's hash is
stored, and the user is re-read on every request (`src/setuhaul/auth/sessions.py:53`).

> ⚠️ Turning the switch off is refused when neither Google nor the break-glass password is on, a reset included
> (`_check_switch` in `src/setuhaul/auth/settings_routes.py`). Turning it on is checked on save only, so that a reset can
> always return to `.env`.

## Password links

- 48 hours for an invitation, 1 hour for a reset; one use (`src/setuhaul/auth/links.py:29`).
- Limits per account and for all forgot-password mail are kept in Redis (`src/setuhaul/auth/links.py:68`).
- Setting a password ends that user's sessions and signs nobody in (`src/setuhaul/auth/links.py:136`).
- Passwords and the break-glass password share one credential table and lock (`src/setuhaul/auth/local_admin.py:97`).

> ⚠️ "Forgot password?" must answer the same for every address, and looks the user up after the reply
> (`src/setuhaul/auth/password_routes.py:141`). Moving the lookup earlier reveals which addresses exist.

> ⚠️ The routes anyone can call accept only `application/json` (`src/setuhaul/auth/password_routes.py:50`). Another
> site's form can send JSON-looking text but cannot set that content type.

> ⚠️ Older links are cancelled only after the new email has gone (`src/setuhaul/auth/links.py:122`). Cancelling first
> leaves the user with nothing when sending fails.

> ⚠️ Log only the exception type when mail fails: library text can echo the message, link included.

> ⚠️ With Redis down, forgot-password mail is not sent, but an administrator's send still goes
> (`src/setuhaul/auth/links.py:96`). Do not make the uncounted public path send.
