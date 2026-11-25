# Partner-warehouse slot booking by email: design

Status: draft for approval (2026-11-24). Size: small-to-medium extension of the existing hold/confirm flow.

## Intent
When a driver's truck needs a new slot at a **partner warehouse** (a facility whose docks SetuHaul does not control),
the service asks the partner by email and books the slot only after the partner's approval email arrives.
The dock board shows such bookings as *Partner warehouse*.

## Goals
- Email request to the partner's active `facility_contacts` row, sent by its own job (the existing `notification_outbox` cannot carry partner recipients: its recipient_type CHECK has no partner value), following the same at-least-once, dedupe-on-key pattern.
- Approval/decline by reply email, matched to a request by reference and sender, through the existing inbound-message path.
- On approval: confirm through the existing confirm path with `booking_source = 'WAREHOUSE'` and the partner's reference in
  `warehouse_confirmation_ref`. The driver's original appointment ends only in that same transaction (existing rule).
- Honest states everywhere: PENDING_PARTNER, APPROVED, DECLINED, EXPIRED. Driver wording is rendered from stored state;
  a pending request is never described as confirmed.
- Real email: configurable SMTP sender and IMAP inbox reader (see Email configuration).
- Demonstrable with or without a real mailbox: a "partner mailbox" panel in the demo console shows the outgoing email and has
  Approve / Decline buttons that inject the reply.

## Non-goals
- Phone/voice calls (deferred; needs a voice integration).
- Partner self-service portal, partner logins, or partners booking on their own initiative.
- Webhook/push inbound mail (e.g. a mail provider's inbound hook); this change polls an inbox.

## Anti-goals
- Never confirm on an ambiguous, unmatched, wrong-sender or duplicate reply: escalate instead.
- Never assume the partner has capacity; no approval means no booking.
- Never let the model decide approval; matching and parsing are deterministic Python.

## Approaches considered
A. Email request, reply-to-approve (**chosen**). Reuses outbox, inbound messages, contacts, WAREHOUSE source.
B. Email with signed approve link: easier to secure, but not the approval email requested.
C. Add phone calls: no voice integration exists; not reliably demonstrable. Deferred.

## Design
- Migration `010_partner_requests.sql`: `facilities.is_partner` flag; `partner_requests` (request_ref unique, shipment_id,
  facility_id, proposed_start/end, status, expires_at, partner_reply_message_id, created/decided timestamps).
- Service module `partner_booking.py`: `request_slot(...)` (writes request + enqueues email in one transaction, one open
  request per shipment), `handle_reply(...)` (match ref + sender against the contact, strict approve/decline keywords),
  `expire_requests(...)` (job, like `expire_holds`).
- Slot at a partner facility: created from the approved proposed window at approval time (our tables hold no dock rows for
  partners). The unique-index/idempotency rules still apply, so a duplicate approval is a no-op.
- Board/API: appointments with source WAREHOUSE render a "Partner warehouse" tag; pending requests show as pending.
- Demo: `scripts/run_demo.py` gains the partner mailbox panel and a seeded partner facility (flagged `is_partner`, source-tagged
  as a fixture so it is never mistaken for SetuHaul data).
- Tests: request creates one pending row and one outbox email; approve books with WAREHOUSE source and replaces the old
  appointment atomically; decline/expire leave the old appointment untouched; wrong sender, unknown ref, duplicate and
  ambiguous replies do not book.

## Email configuration
- Environment only, like the model settings (optional git-ignored `.env`, documented in `.env.example`; secrets never logged
  or committed): `SETUHAUL_SMTP_HOST/PORT/USER/PASSWORD/FROM`, `SETUHAUL_IMAP_HOST/PORT/USER/PASSWORD/FOLDER`,
  `SETUHAUL_PARTNER_EMAIL_MODE` = `simulated` (default) or `live`.
- Sending: a `Sender` for the existing outbox (`jobs/deliver_notifications`) using SMTP over TLS. Subject carries the request
  reference; `Message-ID` is stored so replies can also be matched by `In-Reply-To`.
- Reading: new job `jobs/poll_partner_replies` reads unseen messages over IMAP/TLS, writes each to `inbound_messages` (dedupe
  on Message-ID, so re-polling is safe), then calls `handle_reply`. Processed mails are marked seen; unmatched ones are left
  and escalated, never auto-approved.
- Safety: only replies whose From address equals an active contact of the request's facility count (spoofing risk is noted
  in docs; SPF/DKIM results from the `Authentication-Results` header are required in `live` mode when present, otherwise escalate).
- Tests use fake SMTP/IMAP servers; no test touches a real mailbox. A `scripts/check_email_config.py` sends a test mail
  and reads the inbox so you can verify your settings.

## Calls I made (correct me)
- Gmail is the mail provider: SMTP `smtp.gmail.com:465` (SSL) or `587` (STARTTLS), IMAP `imap.gmail.com:993`, defaults
  pre-filled in `.env.example`. Gmail requires 2-Step Verification plus a 16-character **app password** (your normal password
  will not work); IMAP must be enabled in Gmail settings. The code stays generic SMTP/IMAP, so another provider is a config change.
- Gmail rewrites the From to the authenticated account, so requests go from that mailbox and replies return to it. Replies are
  read from the `INBOX`, filtered by request reference in the subject. Use a dedicated mailbox, not a personal one.
- You create the app password and put it in your own `.env`; it is never typed into chat or committed.
- In `simulated` mode the demo console panel is used; in `live` mode the same panel shows real sent mail and received replies.
- Email only for now; call is deferred.
- Request expiry defaults to 2 hours of the simulated clock (configurable).
- Partner facility is a seeded demo fixture, since the supplied data has only two facilities.
- Driver is told "requested, waiting for the warehouse" while pending, then notified on approve/decline.
