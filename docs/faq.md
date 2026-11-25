# FAQ: questions you may be asked in a demonstration

Answers describe the code as of 2026-11-26. Each answer starts with the short reply you can say aloud. Where the answer is
"no" or "not built", it says so: that is safer than being caught out. Test names in `backticks` are proof you can run:

```powershell
.\.venv\Scripts\python -m pytest -q -k <test_name>
```

**Contents:** [Before you demonstrate](#0-before-you-demonstrate) · [Product](#1-product) · [AI and the model](#2-ai-and-the-model) ·
[Booking and edge cases](#3-booking-and-edge-cases) · [Concurrency and scale](#4-concurrency-and-scale) ·
[Telegram](#5-telegram-integration) · [Personal data](#6-personal-data-pii-and-privacy) · [Security](#7-security-and-access) ·
[Failures and operations](#8-failures-and-operations) · [Facility change, partners, GPS](#9-facility-change-partner-warehouses-gps) ·
[Testing and evidence](#10-testing-and-evidence) · [Hard questions](#11-hard-questions)

## 0. Before you demonstrate

**Five things not to claim**

1. That it has run in production, or with real drivers. It is a prototype.
2. That a real AI model has been proven on the current flow. The recorded real-model runs predate the one-slot flow and are retired.
3. That Hindi and Hinglish wording is approved. No native speaker has reviewed it.
4. That it is compliant with any data-protection law. That has not been assessed.
5. That the warehouse, customer and operations are emailed or texted on a booking. Their notices are queued; no sender is connected.

**Know which "brain" you are showing**

| Setup | Assistant | Notes |
|---|---|---|
| Demo console (`scripts/run_demo.py`) | the model configured in `.env`; with none, the chat is off | what you type goes to that model provider: use made-up text only |
| Telegram trial ([telegram-local-setup.md](telegram-local-setup.md)) | rule-based stand-in | set phrases only; fixed clock, so holds never expire |
| Automated tests and replays | scripted model | no network |

**Rehearsed phrases for the stand-in:** `my truck is 60 minutes late`, `I will be about two hours late`, `No`, `Yes`,
`never mind, keep my original slot`, `Please change facility to Gurugram`. Anything else may get "How late are you, and which
shipment is it?".

## 1. Product

**What problem does it solve?**
A truck is late and its dock appointment no longer works. Today someone phones around. Here the driver sends a message, gets one
feasible slot held, and confirms with "Yes".

**Who uses it?**
Drivers (Telegram only), warehouse authorities (their facilities, on the web), admins (users, drivers, audit) and coordinators
(cases the system hands over).

**What is the end-to-end flow?**
Driver reports a delay → the service works out the arrival time → finds feasible slots → holds the first for 3 minutes → driver
replies Yes → booking confirmed in one database transaction → history and notices written.

**Why one slot instead of a list?**
Drivers read on a phone, often on the road. One held slot with Yes or No is quicker and leaves no doubt which one is meant.
"No" releases it and holds the next.

**Can a driver choose a particular time?**
Not from a list. They can say No to move on. A time window they state is respected by the search.

**Can the driver cancel or keep the original?**
"Keep my original slot" releases the hold and the original stands. Cancelling a confirmed booking is automatic only if the slot is
at least 120 minutes away and the customer commitment is known and penalty-free; otherwise a person decides.

**What does it not do?**
Facility-wide optimisation, priority pre-emption, voice calls, a driver app or web screen, and billing.

## 2. AI and the model

**Where exactly is AI used?**
Only to understand the driver's message and to draft questions and explanations. Plain code decides what is feasible, holds,
confirms, cancels and writes every statement about a booking.

**Can the model book a slot by itself?**
No. A write needs the driver's own words from the current message, one write per message, and confirmation only of a hold placed in
an earlier message. `test_hold_and_confirm_in_the_same_message_is_refused`, `test_a_fabricated_driver_quote_does_not_authorise_a_hold`

**What if the model hallucinates a booking?**
Confirmed, held, released and cancelled wording is rendered from database rows. If model text claims a hold or confirmation that
does not exist, it is replaced. This check is word-pattern based, so a cleverly paraphrased claim could slip past it.
`test_unbacked_confirmation_claims_are_replaced`

**What about prompt injection?**
The model has no driver-id parameter, can only call the tools allowed in the current state, and text from load notes is passed as
data. `test_driver_text_cannot_widen_the_tool_allowlist`, `test_no_tool_schema_even_has_a_driver_id_parameter`

**Can the model see another driver's data?**
No. Every tool checks that the shipment belongs to the verified driver. `test_the_model_cannot_name_a_driver_or_reach_a_foreign_shipment`

**Which models are supported?**
OpenAI and Anthropic adapters behind one gateway. Set `SETUHAUL_MODEL_PROVIDER`, `SETUHAUL_MODEL_ID`, `SETUHAUL_MODEL_API_KEY`.

**Has it run with a real model?**
Yes, on the earlier numbered-menu design (2026-11-20, a handful of single-scenario runs). Those recordings were retired when the flow
changed, so **the current flow has no recorded real-model evidence**.

**What limits runaway cost or loops?**
At most 6 model rounds and 4 tool runs per message, an 8-second call timeout with one retry, a 30-second deadline, 800 output
tokens and a USD 0.05 per-message stop (only if prices are configured). `test_runaway_model_is_stopped_and_handed_off`

**What if the model is down?**
Nothing is booked or held, the driver gets a safe message and a case is opened. If it fails after a hold was placed, the driver is
still told about the real hold. `test_model_outage_before_any_commit_is_safe_and_escalated`

**Why LangGraph and LangSmith?**
LangGraph runs the conversation loop; the caps are enforced by our code, not the graph. LangSmith is optional tracing, off by default.

## 3. Booking and edge cases

**How is a slot judged feasible?**
Dock type and weight limit, unload time fitting one slot, dock blocks, truck able to arrive in time, labour availability and
customer commitment. See [scheduling-algorithm.md](scheduling-algorithm.md).

**What if information is missing?**
It says "cannot confirm", never "assume available". The supplied data has no labour or customer data at all, so on that data alone
every case goes to a person; the demo adds clearly labelled fixture rows. `test_missing_labour_and_consignee_data_is_incomplete_not_feasible`

| The question: "what if…" | What happens | Proof |
|---|---|---|
| two drivers want the same slot | exactly one wins; the database enforces it | `test_ten_synchronized_threads_one_slot_exactly_one_winner` |
| a planner books it through another tool | the same database constraint refuses the loser | `test_external_writer_hits_the_same_uniqueness_constraint` |
| the driver replies too late | the hold lapsed; original booking stands | `test_expired_hold_cannot_be_confirmed_and_original_stands` |
| the driver says No every time | next slot, then a fresh search, then a case | `test_nothing_left_hands_off_and_keeps_the_original_booking` |
| "No, that is fine" | treated as agreement, not a refusal | `test_no_that_is_fine_keeps_the_held_slot` |
| no slot fits (heavy or long load) | handed to a person, nothing promised | `test_when_nothing_fits_there_is_a_hand_off_and_no_menu` |
| the delay is zero or negative | zero keeps the plan; negative is invalid | `test_zero_delay_keeps_planned_eta`, `test_negative_delay_is_invalid` |
| a breakdown with only a repair time | it asks for the drive time too | `test_repair_without_travel_time_asks_a_question_and_does_nothing_else` |
| the driver says late but the gate shows arrived | contradiction, handed to a person | `test_gate_in_contradiction_hands_off` |
| the driver has two loads | it asks which | `test_two_active_shipments_the_driver_is_asked_which` |
| the same message is delivered twice | same reply, one action | `test_duplicate_delivery_returns_the_stored_reply_without_another_model_call` |
| the reply is lost after booking | a retry returns the same result | `test_response_loss_after_commit_is_recoverable_by_retry` |
| the slot stops being valid before "Yes" | hold released, original kept | `test_feasibility_is_rechecked_and_a_lost_slot_releases_the_hold` |
| the original is already being unloaded | it is never ended | `test_an_original_already_in_progress_is_never_ended` |
| confirm fails half way | everything rolls back | `test_failure_at_any_write_point_rolls_everything_back` |
| the slot time has passed | not offered | `test_driver_cannot_book_slot_in_the_past` |
| a message over 2000 characters, or empty | refused before the model | `test_a_message_of_exactly_2000_characters_is_accepted_and_2001_is_refused` |
| a driver floods messages | limited to 20 a minute, 100 an hour | `test_burst_is_rate_limited_before_the_model_is_called` |
| a language other than English, Hindi, Hinglish | one language question, then a person | `test_unsupported_language_gets_one_choice_question_then_a_hand_off` |

The full list is in the [scenario catalogue](scenario-catalogue.md).

**How long is a hold?**
3 minutes for a delay report. 15 minutes for an offer the system makes on its own (closer truck, approved facility change). The
3-minute value is a proposed default awaiting operations approval, and is short for a driver who is driving.

**Can priority loads jump the queue?**
No. First valid hold wins. Priority is advisory.

**What if a truck needs more than one slot?**
It goes to a person. Two sample loads (75 minutes against 60-minute slots) show this.

## 4. Concurrency and scale

**How do you prevent double booking?**
Holds and bookings share one table with unique indexes, and each change is one `BEGIN IMMEDIATE` transaction. Twenty repeated
rounds of threads and processes found 0 invariant violations ([concurrency-proof.md](verification/concurrency-proof.md)).

**What load has it taken?**
On one Windows machine with a stand-in model: slot search p95 12.9 ms, API p95 61 ms, about 245 hold attempts a second
([scale-test.md](verification/scale-test.md)). **Real model latency is not included** and would dominate.

**Where does it stop scaling?**
SQLite allows one writer at a time; waits of several seconds appear from four concurrent writers. Move to PostgreSQL before more
than one writer service or more than four API workers.

**Can I run several server copies?**
Not as built. Telegram sending assumes one process (copies could send duplicates) and the rate limiter is approximate across processes.

**Does it need Redis?**
Yes, for a short conversation cache and sign-in state. The database is the record; a lost cache is rebuilt.
`test_redis_loss_rebuilds_from_sql`. The demos use an in-process stand-in.

## 5. Telegram integration

**Why Telegram?**
Drivers already have it, bots are free and it identifies the sender. WhatsApp or SMS would need a provider account; the
conversation engine does not depend on the channel.

**How does it connect?**
Telegram calls our webhook over HTTPS. We check a secret header before reading the body, then pass the text to the same engine as
every other channel. Setup: [telegram-local-setup.md](telegram-local-setup.md).

**How do you know who the driver is?**
First contact: the bot asks them to tap **Share my phone number**. It links only if the contact is their own and the number matches
exactly one eligible driver. After that the Telegram account is the identity.

**What if someone forwards another person's contact card?**
Refused: the card's owner must be the sender.

**What does a refused person see?**
The same neutral message in every case, so the bot cannot be used to discover which numbers are drivers. The reason is only in the
audit log.

**Stolen or changed phone?**
An admin, or the warehouse authority who onboarded the driver, revokes the link. A revoke is sticky until **Allow re-link**.

**Suspended driver or expired licence?**
Refused at once. Supplied sample drivers have no licence record, so their expiry is not checked.

**Group chats, photos, voice notes, locations, edited messages?**
Only private chats and text messages are handled. Anything else from a linked driver is ignored **without a reply**; edits are
ignored. No location is read from Telegram.

**What if the driver sends too many messages?**
Over the rate limit, nothing is processed and **no reply is sent**.

**What if Telegram sends the same update twice?**
The update id is used as the message id, so it is processed once.

**Does the system send messages on its own?**
Yes: facility change approved or rejected, closer-truck offer, lapsed hold. A sender checks every 5 seconds.

**Do booking confirmations get pushed?**
The driver gets them as the reply to their "Yes". There is no separate push for bookings, cancellations or case updates, and
coordinator updates appear only in the stored chat.

**What if Telegram is unreachable?**
Queued messages get five attempts over about ten minutes (30 s, 1, 3 and 5 minutes), kept in order per driver. A direct reply to a
driver's own message is attempted once. Messages older than 30 minutes are not sent.

**What if our server is down when a driver writes?**
Telegram keeps retrying the webhook for a while. If the database is unavailable we answer 503 so it retries.

**Is the bot token safe?**
It comes from the environment, is never logged, and send errors report only the error type. Anyone who sees it can impersonate the
bot, so revoke it in BotFather if exposed. The HTTP client's own URL logging is silenced because the URL contains the token.

**Has it been run for real?**
Once, 2026-11-25, with a real bot through a temporary tunnel and the stand-in assistant: linking, delay, confirm, facility change,
web approval, pushed message and confirm all worked. Not tried with a real model or a permanent HTTPS address.

## 6. Personal data (PII) and privacy

**What personal data do you hold?**

| Data | Where | Used for | Deleted automatically? |
|---|---|---|---|
| Driver name, phone, home city, licence number | `drivers` | identity, onboarding | no |
| Licence expiry, approval status | `driver_profiles` | eligibility | no |
| Telegram user id and chat id | `telegram_identities` | linking and sending | only a revoked row, on re-link |
| Full chat text | `chat_messages`, cases, Redis cache | conversation, hand-off | database: no. Cache: 24 hours |
| GPS positions | `gps_pings` | arrival check | no |
| Web user email and sessions | `users`, `auth_sessions` | sign-in | sessions expire; rows stay |
| Actor email, IP address, action | `audit_log` | accountability | never (append-only) |
| Sender address of partner mail | partner tables | matching replies | no |
| Booking history | `appointment_events` | audit | never (immutable) |

Only slot-option records are purged (after 30 days).

**What is sent to the AI provider?**
The recent conversation (up to 20 turns or 12,000 characters), load facts such as order reference, weight, times and facility, and
free-text notes on the load. The code adds **no driver name, phone number or licence number**. But there is **no redaction before the
model**: whatever a driver types, or someone wrote in a note, is sent as written.

**What else leaves the system?**

| Recipient | What |
|---|---|
| Telegram | every message between driver and bot, and the phone number the driver shares. Bot chats are not end-to-end encrypted |
| Google | sign-in of web users |
| Partner warehouse email | contact name, facility, order reference, requested time; no driver details |
| LangSmith (off by default) | traces with phones, emails and secret-looking strings masked; names are **not** masked. `SETUHAUL_LANGSMITH_HIDE_TEXT=1` hides message text |
| Tunnel provider (trial only) | the webhook traffic passes through it |

**How is data protected?**
* Identity is verified by the channel or session, never taken from the message.
* Warehouse users see only their facilities, and driver chats only for drivers on their live projects, from the day of allocation.
* Phone numbers are masked in the driver lists; licence numbers are never returned by them.
* Session tokens are stored only as hashes; the backup password is hashed with scrypt.
* Our structured logs mask phone numbers and secrets; recorded test traces are sanitised.

**Is data encrypted?**
In transit to Telegram, Google, mail and model providers, yes (TLS). **At rest, the application does not encrypt**: the database is a
plain SQLite file, so disk encryption and file permissions are the owner's job. The local server speaks HTTP; production needs HTTPS in front.

**Can a driver ask for their data, or to be forgotten?**
**Not built.** There is no export or erase tool, and the history and audit tables forbid changes by design, so an erasure policy
would need a decision (for example pseudonymising).

**Do drivers consent?**
Sharing the phone number is a deliberate tap and the link is audited. There is **no privacy notice or recorded consent text** in the bot.

**Are you compliant with India's DPDP Act, or GDPR?**
Not assessed. Before real drivers: a privacy notice and consent, a retention schedule, access and erasure handling, processor
agreements (model provider, Telegram, hosting), data location, and a breach process. The model provider and data-protection position
is still an open question (Q9 in [operations-questions.md](operations-questions.md)).

**Is real data in the repository?**
No. It holds sample data and generated fixtures. Keys live in a git-ignored `.env`.

## 7. Security and access

**How do staff sign in?**
Google, by invitation only. An admin may also have a backup password: off by default, locked for 15 minutes after five failures,
one-hour session, every attempt audited. Google sign-in has **not** been run against real Google.

**What can each role do?**
Admin: everything. Warehouse authority: their facilities. Drivers: only their own loads, by Telegram. Every route is classified in a
role test, and out-of-scope items answer "not found".

**Web attacks?**
Server-rendered pages with escaping, a strict content security policy, HttpOnly SameSite cookies and a same-origin check on changes.
No penetration test has been done, and the web sign-in pages have no general rate limit beyond the password lockout.

**How are other channels protected?**
The signed driver and GPS channel needs an HMAC of the raw body, and refuses everything if no secret is set.
`test_a_forged_driver_request_is_refused_when_no_secret_is_set`. Coordinator service calls need a bearer token, which is not limited to a facility.

**Is the audit trail tamper-proof?**
The database forbids updates and deletes through triggers. Someone with file access to the database could still bypass them.

## 8. Failures and operations

**Database down?** 503 at the door so the sender retries; if it fails mid-message, nothing is promised and the hand-off is written to a file.
`test_database_outage_mid_turn_promises_nothing_and_records_the_hand_off_elsewhere`

**Server crashes while processing?** The message claim is taken over after 60 seconds. `test_a_message_still_being_processed_is_reported_and_a_stale_claim_is_taken_over`

**How does a person get involved?** A case with a reference, the reason and the recent messages. A coordinator claims and resolves it
and the note appears in the driver's chat. No response time is promised, because none has been approved.

**Monitoring and alerting?** Health and readiness endpoints, request ids and structured logs. **No dashboards or alerts.**

**Backups, high availability, deployment?** **Not built.** One server process, one SQLite file, run locally. There is no container image
or deployment pipeline, and the CI workflow in the repository is not being run.

**Can I accidentally damage the sample database?** The server and password script refuse to run on it.
`test_the_server_does_not_start_on_the_sample_database`

## 9. Facility change, partner warehouses, GPS

**Can a driver change warehouse?**
They can ask. A warehouse user of the destination, or an admin, approves or rejects on the web. Nothing changes until then. On
approval a slot is held for 15 minutes; if none exists or the driver does not take one, the load goes back to its original
warehouse and booking. The request expires after 120 minutes.

**Partner warehouses we do not control?**
The service emails them and books only after a clear APPROVE from a known contact that passes mail authentication. Anything unclear
goes to a person. Mail is simulated by default and has not been run against a real mailbox.

**What does GPS do?**
It can show a truck cannot reach a slot, and release the hold. It never overrides the driver to make arrival earlier than they said.
A much closer truck may be offered one earlier slot. Pings older than 15 minutes are ignored.

**Can GPS be spoofed?**
A fake position could cause a hold to be released or an earlier slot offered, but never a booking without the driver's "Yes".
Pings arrive only over the signed channel.

## 10. Testing and evidence

**How much testing is there?**
1364 automated tests, linting and strict type checks; a 23-check end-to-end scenario; 6 replayed traces with 58 evaluator checks;
concurrency and scale scripts. Run them: [running-and-demonstrating.md](running-and-demonstrating.md).

**What has never been tested?**
Real drivers, real labour or customer data, real model on the current flow, real Google sign-in, real partner mailbox, production
HTTPS, multiple servers, a real Redis under load, Linux for the latest code, and native-speaker review.

## 11. Hard questions

**"Is this production ready?"** No. It is a working prototype with the booking rules enforced and tested.

**"What would it take?"** In order: data-protection decision and privacy notice; a labour and customer data source; real-model
evaluation on the current flow; native-speaker review; HTTPS hosting, backups and monitoring; PostgreSQL if load needs it; real
senders for warehouse and customer notices; operations approval of the defaults.

**"What is the business benefit, in numbers?"** Not measured. The data is 21 shipments on one day, which cannot support a claim about
time saved or resolution rate.

**"Why not let the model do everything?"** A wrong booking has a real cost. Rules in code can be tested; model behaviour can only be sampled.

**"What was the weakest thing you found?"** Say it plainly: examples include a signed channel that accepted empty-key signatures when no
secret was set, and Telegram sends giving up within seconds. Both were found and fixed, with tests.

**"What are you least sure about?"** Real-model behaviour on the current flow, and data protection.

**If you do not know an answer:** "I will check the code and come back to you." The [decision register](decision-register.md) records
why most choices were made.
