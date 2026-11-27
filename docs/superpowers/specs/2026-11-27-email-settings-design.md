# Email settings on Admin → Configuration

Status: **design approved** in conversation ("mailbox only"); implementation not started. Lines marked *(proposed)* were
not discussed.

## Intent

An admin sets up the mailbox that sends invitation and password emails from the screen, tests it, and sees why emails are off.

## What people see

A fourth block, **Email**, on Admin → Configuration:

| Field | Setting saved |
|---|---|
| Mail server, port | `SETUHAUL_SMTP_HOST`, `SETUHAUL_SMTP_PORT` |
| Sending user, app password, from address | `SETUHAUL_SMTP_USER`, `_PASSWORD`, `_FROM` |

Buttons: **Save**, **Send test email to me**, **Reset to .env**.

Above the fields, a read-only status: sign-in emails **on** or **off**, and if off, what is missing (names only). It also
shows whether `SETUHAUL_PASSWORD_LOGIN` is set and the site address that password links use. The Users tab says "Invitation
emails are off: see Email under Configuration." when they are.

## Stays in `.env`

`SETUHAUL_PASSWORD_LOGIN`, `SETUHAUL_PUBLIC_URL` and `SETUHAUL_SETTINGS_KEY`. Password links are built from the site
address, so it cannot be changed from a screen.

## Safeguards

- The app password is encrypted with `SETUHAUL_SETTINGS_KEY` and never shown. Without that key it cannot be saved.
- Changing the mail server, port or sending user requires typing the password again, so a stored password is never sent to a
  new server.
- The test email goes only to the signed-in admin's own address, at most once a minute *(proposed)*.
- Every save, reset and test is audited; server and addresses with old and new values, the password never.

## Behaviour

- Saved values apply at once, without a restart, and override `.env` until **Reset to .env**.
- With nothing saved, behaviour is exactly as today.
- Partner warehouse email keeps using `.env` only *(proposed)*.

## Not included

Switching sign-in on or setting the site address from the screen, reading mail (IMAP), email wording, the demo server.

## Approaches

| Approach | Trade-off |
|---|---|
| **Mailbox on screen; switch and address in `.env` (picked)** | A one-time server edit; links cannot be redirected from the screen. |
| Everything on screen | No `.env` editing, but any admin account could point password links elsewhere. Not picked. |

## Server requirement

Set the three `.env` values once. The server must reach the mail port; not tried against a real mail server.
