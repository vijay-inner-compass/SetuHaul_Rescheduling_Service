# Telegram delivery of queued driver notices: design

Status: approved by the owner (2026-11-25). Size: small change.

## Why
Messages the system writes on its own (facility approval or rejection, closer-truck offer, lapsed hold) are queued in
`notification_outbox`, and `jobs/send_driver_notifications.py` can send them, but nothing runs it. A Telegram driver
sees only replies to their own messages, and an offered slot lapses unseen.

## Goals
- When the bot is configured, queued driver notices reach the driver's chat within seconds, with nothing extra to run.

## Non-goals
- Booking or cancellation confirmations (drivers get those as replies). Running several server copies. A command-line sender.

## Approaches
- **A loop inside the server (chosen):** like the hold sweep; nothing to install or schedule. One process only.
- **A separate scheduled command:** suits several server copies, but is one more thing to set up and forget.

## Design
- Bot configured: the server sends every 5 seconds and also runs the hold sweep (which sends unbooked moves back).
- A notice more than 30 minutes old is not sent; it is marked `PERMANENT_FAILURE` with `TOO_OLD`. This stops a backlog
  reaching drivers on first run; offers last 15 minutes.
- Failed sends retry up to `notification_max_attempts`. An unlinked or ineligible driver gets nothing.
- Errors are logged without the bot token and never affect requests or stop the loop.
- Tests use the fake Telegram client; nothing real is sent.
