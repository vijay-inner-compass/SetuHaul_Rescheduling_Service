# Partner-Warehouse Email Booking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a truck needs a new slot at a partner warehouse, email the partner (Gmail SMTP), read the approval reply (Gmail IMAP), book only on a verified approval, and show the booking on the dock board as "Partner warehouse".

**Architecture:** A new `partner_requests` table and `partner_booking.py` service own the lifecycle PENDING_PARTNER → APPROVED / DECLINED / EXPIRED. Email I/O sits behind two injectable interfaces (`MailSender`, `MailReader`) so tests and the demo use in-memory fakes and `live` mode uses `smtplib`/`imaplib`. Approval books through one SQLite transaction that mirrors `confirm_reschedule` (end original, create CONFIRMED appointment with source `WAREHOUSE`), reusing `append_appointment_event`.

**Tech Stack:** Python 3.12, SQLite, FastAPI demo console, pytest, stdlib `smtplib`/`imaplib`/`email` (no new dependencies).

**Spec:** `docs/superpowers/specs/2026-11-24-partner-warehouse-booking-design.md`

## Global Constraints
- Config from environment only; secrets never logged, committed or echoed: `SETUHAUL_SMTP_HOST/PORT/USER/PASSWORD/FROM`, `SETUHAUL_IMAP_HOST/PORT/USER/PASSWORD/FOLDER`, `SETUHAUL_PARTNER_EMAIL_MODE` = `simulated` (default) | `live`.
- Gmail defaults: SMTP `smtp.gmail.com` 465 (SSL) or 587 (STARTTLS); IMAP `imap.gmail.com` 993; app password required.
- Approval is deterministic Python only; the model never decides it. Unmatched, wrong-sender, duplicate or ambiguous replies never book; they open an escalation case.
- One open partner request per shipment. Request expiry default 2 hours (simulated clock), configurable.
- Appointment source is `WAREHOUSE`; partner reference goes in `appointments.warehouse_confirmation_ref`.
- The original appointment ends only inside the transaction that books the replacement.
- Wording shown to drivers is rendered from stored state; a pending request is never called confirmed.
- No test touches a real mailbox or network. Seeded partner facility is tagged as a fixture, never as SetuHaul data.
- Follow repo style: `from __future__ import annotations`, line-level comments, `ruff` and `mypy src` clean.

## Review Focus
- Reply from the right address but for a *different* request ref: must not book (Task 3).
- Same approval email delivered or polled twice: exactly one booking (Task 3, Task 4).
- Approval arrives after expiry, or after the driver's original appointment started unloading (IN_PROGRESS): no booking, escalate (Task 3).
- Reply quoting the original text ("... reply APPROVE or DECLINE") containing both words, or "not approved": ambiguous means no booking (Task 3).
- Slot at the partner already taken when approval arrives: unique index rejects it, request ends unbooked and escalates (Task 3).

---

### Task 1: Schema and email settings

**Files:**
- Create: `migrations/010_partner_requests.sql`, `src/setuhaul/mailcfg.py`
- Modify: `.env.example`
- Test: `tests/test_partner_schema.py`, `tests/test_mailcfg.py`

**Interfaces:**
- Produces: table `partner_requests(request_ref TEXT PK, shipment_id, facility_id, contact_id, proposed_start TEXT, proposed_end TEXT, status CHECK IN ('PENDING_PARTNER','APPROVED','DECLINED','EXPIRED','FAILED'), email_status CHECK IN ('PENDING','SENT','FAILED'), email_attempts INT, message_id TEXT, slot_id TEXT, appointment_id TEXT, reply_message_id TEXT, created_at, expires_at, decided_at)`; unique partial index on `shipment_id` where `status='PENDING_PARTNER'`; `facilities.is_partner INTEGER NOT NULL DEFAULT 0`; table `partner_inbound(message_id TEXT PK, request_ref TEXT, from_addr TEXT, outcome TEXT, received_at TEXT)`.
- Produces: `@dataclass(frozen=True) class MailSettings` with `mode, smtp_host, smtp_port, smtp_user, smtp_password, smtp_from, imap_host, imap_port, imap_user, imap_password, imap_folder, request_ttl_min`; `def load_mail_settings() -> MailSettings`; `MailSettings.__repr__` masks passwords.

- [ ] **Step 1: Write failing tests.** `test_partner_schema.py::test_second_pending_request_for_same_shipment_is_rejected` (insert two PENDING_PARTNER rows for one shipment, expect `sqlite3.IntegrityError`; a DECLINED plus a PENDING row is allowed) and `::test_is_partner_defaults_to_zero`. `test_mailcfg.py::test_defaults_are_simulated_with_gmail_hosts`, `::test_live_mode_requires_user_and_password` (raises `ValueError` naming the missing variable, not its value), `::test_repr_hides_password`.
- [ ] **Step 2: Run** `.\.venv\Scripts\python -m pytest tests/test_partner_schema.py tests/test_mailcfg.py -q`. Expected: FAIL (no table/module).
- [ ] **Step 3: Implement** the migration (applied automatically by `make_working_db` in filename order) and `mailcfg.py` per the interfaces; add the commented Gmail block to `.env.example` (host/port defaults, "use an app password, never your normal password").
- [ ] **Step 4: Run** the same command plus `.\.venv\Scripts\python -m pytest -q`. Expected: all pass (existing 494 unaffected).
- [ ] **Step 5: Commit** `feat: partner_requests schema and Gmail mail settings`.

### Task 2: Request creation and expiry

**Files:**
- Create: `src/setuhaul/partner_booking.py`, `src/setuhaul/jobs/expire_partner_requests.py`
- Test: `tests/test_partner_request.py`

**Interfaces:**
- Consumes: `Clock`, `immediate()` from `setuhaul.db`, `facility_contacts` rows, `MailSettings.request_ttl_min`.
- Produces: `class PartnerRequestResult(BaseModel)` with `status: Literal["REQUESTED","ALREADY_PENDING","NOT_PARTNER_FACILITY","NO_PARTNER_CONTACT","SHIPMENT_NOT_RESCHEDULABLE"]`, `request_ref: str | None`.
- Produces: `def request_slot(conn, clock, *, shipment_id: str, proposed_start: datetime, proposed_end: datetime, ttl_min: int = 120) -> PartnerRequestResult`; `def expire_partner_requests(conn, clock) -> int` (returns count expired); `def render_request_email(conn, request_ref: str) -> tuple[str, str, str]` returning `(to_addr, subject, body)`; subject is exactly `f"SetuHaul slot request [{request_ref}]"`; body states shipment order reference, window and "Reply APPROVE or DECLINE".
- `request_ref` format `PR-` + 8 uppercase hex chars.

- [ ] **Step 1: Write failing tests:** `test_request_creates_one_pending_row_and_picks_active_contact`, `test_second_request_same_shipment_returns_ALREADY_PENDING`, `test_non_partner_facility_returns_NOT_PARTNER_FACILITY`, `test_facility_without_active_contact_returns_NO_PARTNER_CONTACT`, `test_expire_marks_overdue_pending_as_EXPIRED_and_leaves_original_appointment_untouched`, `test_email_subject_contains_ref_and_body_has_no_driver_phone_number` (privacy: no driver phone in body).
- [ ] **Step 2: Run** `pytest tests/test_partner_request.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the three functions. `request_slot` is one `immediate(conn)` transaction; contact choice: first active contact with an email, role order WAREHOUSE_COORDINATOR first.
- [ ] **Step 4: Run** the test file. Expected: PASS.
- [ ] **Step 5: Commit** `feat: create and expire partner slot requests`.

### Task 3: Reply handling and atomic partner booking

**Files:**
- Modify: `src/setuhaul/partner_booking.py`
- Test: `tests/test_partner_reply.py`

**Interfaces:**
- Consumes: Task 2 functions; `append_appointment_event`, `create_handoff`/`HandoffRequest` (reason code `OTHER_UNCERTAINTY`), `AuthError` not needed.
- Produces: `def parse_decision(body: str) -> Literal["APPROVE","DECLINE","AMBIGUOUS"]` (looks only at the new text above the first quoted line starting with `>` or `On ... wrote:`; whole-word, case-insensitive; both words or negations such as "not approve" → AMBIGUOUS).
- Produces: `class ReplyOutcome(BaseModel)` with `status: Literal["BOOKED","DECLINED","IGNORED_DUPLICATE","ESCALATED"]`, `reason: str | None`, `appointment_id: str | None`.
- Produces: `def handle_reply(conn, clock, *, message_id: str, from_addr: str, subject: str, in_reply_to: str | None, body: str, authentication_results: str | None) -> ReplyOutcome`.
- Request match: ref parsed from subject by regex `\[(PR-[0-9A-F]{8})\]`, falling back to `in_reply_to == partner_requests.message_id`. Sender must equal (case-insensitive) an active contact email of the request's facility. If `authentication_results` is present and contains `spf=fail` or `dkim=fail` or `dmarc=fail`, escalate.
- Booking transaction on APPROVE: re-check status is PENDING_PARTNER and `expires_at` not passed; create `appointment_slots` row (id `PSLOT-<ref>`, partner facility dock, window from request) and a CONFIRMED appointment (`booking_source='WAREHOUSE'`, `warehouse_confirmation_ref=<message_id>`); end the shipment's original appointment (`CANCELLED`, reason `RESCHEDULED`) unless it is IN_PROGRESS (then escalate, book nothing); event type `PARTNER_BOOKING_CONFIRMED`; update request to APPROVED. `partner_inbound` row (PK `message_id`) written in the same transaction makes a repeat delivery return `IGNORED_DUPLICATE`.

- [ ] **Step 1: Write failing tests:** `test_approve_books_with_warehouse_source_and_ends_original_atomically`; `test_fault_after_original_ended_rolls_back_everything` (monkeypatch a failure inside the transaction; original still current, request still pending); `test_decline_marks_declined_and_keeps_original`; `test_same_message_twice_books_once` (second call `IGNORED_DUPLICATE`); `test_wrong_sender_escalates_and_does_not_book`; `test_valid_sender_but_other_requests_ref_does_not_book_this_request`; `test_unknown_ref_escalates`; `test_approval_after_expiry_does_not_book`; `test_original_in_progress_escalates_without_booking`; `test_slot_already_taken_ends_request_FAILED_and_escalates`; `test_spf_fail_escalates`; parametrized `test_parse_decision` over `"APPROVE"`, `"approved, thanks"`, `"Decline"`, `"not approved"`, `"Reply APPROVE or DECLINE"` (quoted instructions only), `"APPROVE\n> Reply APPROVE or DECLINE"`, `"ok"`, expecting APPROVE, APPROVE, DECLINE, AMBIGUOUS, AMBIGUOUS, APPROVE, AMBIGUOUS.
- [ ] **Step 2: Run** `pytest tests/test_partner_reply.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** `parse_decision`, `handle_reply`, and the booking transaction (module-private `_book_partner_slot`), with a `_fault(point)` hook like `booking._fault` so the rollback test can force a crash.
- [ ] **Step 4: Run** the test file, then the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `feat: verified partner reply books slot atomically`.

### Task 4: Gmail SMTP sender and IMAP poller

**Files:**
- Create: `src/setuhaul/mail_transport.py`, `src/setuhaul/jobs/send_partner_requests.py`, `src/setuhaul/jobs/poll_partner_replies.py`, `scripts/check_email_config.py`
- Test: `tests/test_mail_transport.py`, `tests/test_partner_jobs.py`

**Interfaces:**
- Produces: `class OutgoingMail(BaseModel)` (`to_addr, subject, body, message_id`); `class IncomingMail(BaseModel)` (`message_id, from_addr, subject, in_reply_to, body, authentication_results`); `class MailSender(Protocol): def send(self, mail: OutgoingMail) -> None`; `class MailReader(Protocol): def fetch_unseen(self) -> list[IncomingMail]; def mark_seen(self, message_id: str) -> None`.
- Produces: `SmtpSender(settings)` (port 465 → `SMTP_SSL`, else `STARTTLS`; login with user/password; 30 s timeout), `ImapReader(settings)` (`IMAP4_SSL`, folder from settings, searches UNSEEN, uses `BODY.PEEK[]` so nothing is marked seen until `mark_seen`; extracts the text/plain part), `InMemoryMailbox` (implements both, for tests and simulated mode; has `.sent: list[OutgoingMail]` and `.inject(IncomingMail)`), `def build_mail(settings) -> tuple[MailSender, MailReader]` (simulated → one shared `InMemoryMailbox`).
- Produces: `def send_pending_requests(conn, clock, sender: MailSender) -> int` (for each request with `email_status='PENDING'`: build mail from `render_request_email`, generate `message_id`, send, store `message_id`, set SENT; on exception increment `email_attempts`, set FAILED after 5, never raise, never include credentials in `last_error`); `def poll_partner_replies(conn, clock, reader: MailReader) -> int` (for each unseen mail call `handle_reply`, then `mark_seen` only after the call returned, so a crash re-reads it and the `partner_inbound` primary key makes that safe; mail with no matching ref still goes to `handle_reply` and escalates).
- `scripts/check_email_config.py`: loads settings, in `live` mode sends one test mail to the configured user and reads the inbox, printing OK/failure reasons without printing the password.

- [ ] **Step 1: Write failing tests** using a fake `smtplib.SMTP_SSL` / `imaplib.IMAP4_SSL` (monkeypatch): `test_smtp_ssl_used_for_465_and_starttls_for_587`, `test_smtp_failure_marks_attempt_and_error_text_has_no_password`, `test_imap_reader_parses_plain_text_and_authentication_results`, `test_imap_fetch_uses_peek_so_message_stays_unseen`, `test_send_pending_requests_sends_once_and_stores_message_id`, `test_poll_books_on_valid_reply_and_marks_seen`, `test_poll_twice_same_mail_books_once`, `test_poll_crash_before_mark_seen_rereads_without_double_booking`, `test_simulated_mode_never_opens_a_socket` (patch `socket.socket` to fail).
- [ ] **Step 2: Run** `pytest tests/test_mail_transport.py tests/test_partner_jobs.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** per the interfaces. Use stdlib `email.message.EmailMessage` and `email.policy.default`; strip HTML-only mails to empty body (then AMBIGUOUS).
- [ ] **Step 4: Run** both test files and full suite; run `ruff check .` and `mypy src`. Expected: clean.
- [ ] **Step 5: Commit** `feat: Gmail SMTP sender and IMAP reply poller for partner requests`.

### Task 5: Board tag and driver wording

**Files:**
- Modify: `src/setuhaul/demo_ui.py` (board query near `owner`/`slots`), `src/setuhaul/language.py` or the existing state-rendered wording module used for hold/confirm text (find with `grep -n "Held\|hold" src/setuhaul/language.py src/setuhaul/tools.py`)
- Test: `tests/test_partner_surface.py`

**Interfaces:**
- Consumes: `partner_requests`, `appointments.booking_source`.
- Produces: each board slot dict gains `source: str | None` (`"WAREHOUSE"` when the occupying appointment has that source) and `partner: bool`; a pending request is returned in a new `pending_partner` list on `/demo/api/board?facility_id=...` with `request_ref, order_reference, window, expires_at`.
- Produces: `def render_partner_status(conn, shipment_id: str) -> str | None` returning wording from stored state in en/hi: PENDING_PARTNER "requested, waiting for the warehouse", APPROVED states the confirmed time, DECLINED/EXPIRED says the original appointment is unchanged.

- [ ] **Step 1: Write failing tests:** `test_board_marks_warehouse_source_slot_as_partner`, `test_board_lists_pending_request_separately_not_as_booked`, `test_pending_wording_never_says_confirmed`, `test_declined_wording_says_original_unchanged`.
- [ ] **Step 2: Run** `pytest tests/test_partner_surface.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the query/serialization changes and `render_partner_status` (Hindi text marked in the docs as unreviewed by a native speaker, like existing Hindi).
- [ ] **Step 4: Run** the test file and `tests/test_demo_ui.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: show partner-warehouse bookings and pending requests on the board`.

### Task 6: Demo console partner mailbox and fixture

**Files:**
- Modify: `src/setuhaul/demo_ui.py` (`seed_demo_evidence`, `register_demo`), `src/setuhaul/demo/index.html`, `scripts/run_demo.py`
- Test: `tests/test_demo_partner.py`

**Interfaces:**
- Produces: fixture facility `FAC-PARTNER-01` (`is_partner=1`, one dock, two open slots, active contact `partner.desk@partner.example`), rows tagged in a `source`-style comment/column consistent with `006_labour_consignee.sql`.
- Produces endpoints (localhost only, like existing demo endpoints): `POST /demo/api/partner-request {shipment_id, proposed_start, proposed_end}`; `GET /demo/api/partner-mailbox` → `{mode, sent: [...], inbox: [...], requests: [...]}`; `POST /demo/api/partner-reply {request_ref, decision: "APPROVE"|"DECLINE"}` (simulated mode only; injects an `IncomingMail` from the contact address then runs `poll_partner_replies`); `POST /demo/api/partner-sync` (runs send then poll once; used in `live` mode and by a "Check mailbox" button).
- UI: a "Partner mailbox" panel with the outgoing email text, Approve and Decline buttons (simulated mode), a "Check mailbox" button, and the board's partner tag styled distinctly.

- [ ] **Step 1: Write failing tests** with the FastAPI test client: `test_request_then_approve_shows_partner_booking_on_board`, `test_decline_leaves_original_booking_on_board`, `test_partner_reply_endpoint_rejected_in_live_mode`, `test_partner_endpoints_reject_non_local_clients`, `test_reset_clears_partner_requests_and_mailbox`.
- [ ] **Step 2: Run** `pytest tests/test_demo_partner.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** fixture seeding, endpoints, panel, and wire `build_mail(load_mail_settings())` into `register_demo`; extend demo reset to clear new tables.
- [ ] **Step 4: Run** the new tests, `tests/test_demo_ui.py`, the full suite, then start `python scripts/run_demo.py` and check the browser flow once: request → email shown → Approve → board shows the partner tag. Expected: all pass; flow works.
- [ ] **Step 5: Commit** `feat: partner mailbox panel and fixture in the demo console`.

### Task 7: Conversation trigger and docs

**Files:**
- Modify: `src/setuhaul/tools.py` (follow the existing tool pattern and `docs/model-contract.md` schema), `src/setuhaul/stub_model.py` only if its scripted turns need the new tool, `README.md`, `docs/running-and-demonstrating.md`, `docs/decision-register.md`, `docs/scenario-catalogue.md`, `.env.example` (already from Task 1)
- Test: `tests/test_partner_tool.py`

**Interfaces:**
- Consumes: `request_slot`, identity-verified driver and shipment from the tool context (identity never comes from the model).
- Produces: model tool `request_partner_slot(option_start: str)` that, for a shipment whose destination facility `is_partner`, calls `request_slot` for that driver's own shipment and returns the `render_partner_status` wording; refuses (structured business answer) for non-partner facilities and for shipments not belonging to the verified driver.

- [ ] **Step 1: Write failing tests:** `test_tool_requests_partner_slot_for_own_shipment`, `test_tool_refuses_other_drivers_shipment`, `test_tool_refuses_non_partner_facility`, `test_tool_answer_is_rendered_from_stored_state`.
- [ ] **Step 2: Run** `pytest tests/test_partner_tool.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the tool and register it with the model gateway schema; update `docs/model-contract.md` tool list.
- [ ] **Step 4: Docs:** add a "Partner warehouses by email" section to `README.md` and `docs/running-and-demonstrating.md` (Gmail setup: 2-Step Verification, app password, enable IMAP, `.env` keys, `scripts/check_email_config.py`), a decision-register entry (including why partner mail uses its own columns and job instead of `notification_outbox`: its `recipient_type` CHECK has no partner value and rebuilding that table was judged riskier), and two scenario-catalogue rows (approve, ambiguous reply).
- [ ] **Step 5: Run** the full suite, `ruff check .`, `mypy src`. Expected: all green.
- [ ] **Step 6: Commit** `feat: partner slot request tool and docs`.

---

## Self-review notes
- Spec coverage: request email (T2, T4), reply approval (T3, T4), Gmail config and check script (T1, T4), board tag and honest wording (T5), demo panel and fixture (T6), expiry (T2), escalation on bad replies (T3), docs (T7). Phone calls are out of scope per the spec.
- **Deviation from the spec:** the spec says requests go "through the existing outbox". `notification_outbox.recipient_type` has a CHECK with no partner value, so this plan keeps send state on `partner_requests` and adds its own job. The spec should be read with that change; the decision-register entry in Task 7 records it.
- Names are consistent across tasks: `request_slot`, `handle_reply`, `parse_decision`, `MailSender`/`MailReader`, `InMemoryMailbox`, `build_mail`, `render_partner_status`.
