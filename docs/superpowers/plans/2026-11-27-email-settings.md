# Email Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Admins set up and test the password-email mailbox from Admin → Configuration.

**Architecture:** A new `email` section in the existing saved-settings layer. Password routes and `links.send` ask one function
for the mailbox in force instead of using the sender fixed at start-up.

**Tech Stack:** Python 3.12, FastAPI, SQLite, smtplib (existing `SmtpSender`), pytest.

**Spec:** `docs/superpowers/specs/2026-11-27-email-settings-design.md`

## Global Constraints

- Branch `feat/email-settings` from `main`. Do not push.
- No new dependencies and no migration.
- `SETUHAUL_PASSWORD_LOGIN`, `SETUHAUL_PUBLIC_URL`, `SETUHAUL_SETTINGS_KEY` are never saved or overridden by the screen.
- A password, link or token never appears in a response, page, log or audit row.
- With nothing saved, behaviour is unchanged. Tests never reach a real mail server.
- No inline script or style. Finish with ruff, mypy and `pytest -q`.

## Review Focus

1. Mail server changed while a password is stored and none typed: refused, nothing saved (Task 1 test).
2. Settings key missing or changed: password reads as unreadable, emails off, screen says so (Task 2 test).
3. Database unreadable while drawing the sign-in page: page still renders from `.env` values (Task 2 test).
4. Port `0`, `65536` or text: refused (Task 1 test).
5. Test email fails: the message names the kind of failure, never the credentials (Task 3 test).

## Tasks

### Task 1: Store and validate the Email section

**Files:** Modify `src/setuhaul/app_settings.py`, `auth/settings_routes.py`; test `tests/test_email_settings.py`.

**Interfaces (produces):**
- `app_settings.SECTIONS["email"] = ("SETUHAUL_SMTP_HOST", "SETUHAUL_SMTP_PORT", "SETUHAUL_SMTP_USER",
  "SETUHAUL_SMTP_PASSWORD", "SETUHAUL_SMTP_FROM")`; the password joins `SECRETS`.
- `EmailBody(host, port, user, password, sender: str | None)`, served by the existing `POST /v1/admin/settings/email` and
  `/email/reset`.
- Refusals: `INVALID_PORT` (not 1-65535), `INVALID_EMAIL` (user, from), `SMTP_PASSWORD_REQUIRED`, `SETTINGS_KEY_UNAVAILABLE`.

- [ ] Write tests: `test_saved_email_settings_override_env`, `test_password_is_encrypted_and_never_returned`,
  `test_changing_server_port_or_user_requires_the_password`, `test_port_and_address_rules`, `test_only_admin_may_save`,
  `test_audit_shows_server_and_addresses_but_not_the_password`.
- [ ] Run (FAIL), implement, run with `tests/test_app_settings.py tests/test_settings_api.py` (PASS), commit
  `feat: store email settings from the admin screen`.

### Task 2: Send with the mailbox in force

**Files:** Modify `auth/account_mail.py`, `auth/links.py`, `auth/password_routes.py`, `auth/password_pages.py`,
`auth/admin_api.py`, `auth/pages.py`, `api.py`; test `tests/test_email_settings.py`.

**Interfaces (produces):**

```python
@dataclass(frozen=True)
class PasswordMail:
    sender: MailSender | None
    missing: tuple[str, ...]          # setting names only

def AppDeps.password_mail(self) -> PasswordMail      # saved over env; env alone if the database cannot be read
```

`deps.password_sender`, when given (tests), still wins. `password_login_on` is `password_mail().sender is not None and
bool(public_url)`. `SmtpSender` is built through a factory on `SettingsSource` (`mail`) so tests can substitute
`InMemoryMailbox`.

- [ ] Write tests: `test_saving_the_mailbox_turns_emails_on_without_restart`, `test_switch_off_in_env_keeps_emails_off`,
  `test_unreadable_password_turns_emails_off`, `test_sign_in_page_renders_when_database_fails`,
  `test_nothing_saved_behaves_as_before`.
- [ ] Run (FAIL), implement, run with `tests/test_password*.py tests/test_auth_pages.py tests/test_admin_api.py` (PASS),
  commit `feat: send password emails with the mailbox in force`.

### Task 3: Screen and test email

**Files:** Modify `auth/pages.py` (`_configuration_section`, `_users_section`), `auth/settings_routes.py`,
`auth/assets/settings.js`, `auth/assets/common.js` (sentences), `auth/wiki_pages.py`, `docs/deployment-hostinger.md`,
`docs/running-and-demonstrating.md`, `.env.example`, `tests/test_auth_role_matrix.py`.

**Interfaces:**
- `POST /v1/admin/settings/email/test` (`ADMIN`): body `EmailBody`; sends to the signed-in admin's address with the values
  given over those in force (the same password rule as saving); returns `{ok, message}`, the message naming the failure type
  only; saves nothing. One per minute (Redis), refusal `TOO_SOON`.
- Configuration shows the status line from the spec; the Users tab shows its sentence when `password_login_on` is false.

- [ ] Write tests: `test_test_email_goes_only_to_the_admin`, `test_a_failed_test_hides_credentials`,
  `test_second_test_within_a_minute_is_refused`, `test_screen_shows_what_is_missing`, `test_users_tab_says_emails_are_off`;
  add the route to `ROLE_GUARDED`.
- [ ] Run (FAIL), implement, run (PASS). Browser check with a stand-in mailbox: save, test, invite.
- [ ] ruff, mypy, `pytest -q` pass; commit `feat: email settings on the Configuration tab`.
