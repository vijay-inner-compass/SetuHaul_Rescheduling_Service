# SetuHaul Rescheduling Service: Knowledge Transfer

**Audience:** an engineer joining the project with no history. **Goal:** after reading this and doing the exercises in
section 14 you can explain the system, run it, change it safely, and know what is and is not proven.

Companion documents: [Running and demonstrating the program](running-and-demonstrating.md) and the [FAQ](faq.md).
Deeper per-layer notes: [code-walkthrough/](code-walkthrough/README.md). Every source file also carries line-by-line comments.

Figures in this document were re-checked on 2026-11-27 on Windows with Python 3.12 unless a date is given.
Python 3.12; section 13 lists where the repository's own older text disagrees.

---

## 1. The problem and the business background

SetuHaul runs warehouses (two in the supplied data: Jaipur and Gurugram). Trucks arrive for **dock appointments**: a truck
is assigned a dock and a one-hour slot. Things go wrong: a truck is late, breaks down, or arrives early. Then the booked slot no
longer works, and today a **warehouse planner or coordinator phones around** to find another slot. During busy periods several
trucks compete for the same few slots, and drivers get inconsistent promises.

**What the service does:** the driver sends a chat message ("my truck is 60 minutes late", in English, Hindi or Hinglish).
The service works out the real arrival time, finds dock slots that can genuinely take that truck, **holds** the first for 3 minutes
("No" moves to the next), **confirms** it when the driver agrees, ends the old appointment, and queues notices for the
planner, the customer (consignee) and operations. When it cannot act safely it opens a **case** for a human coordinator
with full context instead of guessing. Drivers use Telegram; warehouse staff, gate security and admins use signed-in web screens.

**The design stance that explains everything else:** a language model is good at understanding messy messages and bad at being
trusted with bookings. So the model *interprets and drafts*; deterministic Python and the database *decide and state facts*.

| Model does | Code and database do |
|---|---|
| Understand English, Hindi, Hinglish | Know who the driver is |
| Ask a clarifying question | Work out the ETA and which slots are feasible |
| Choose which read tool to call | Hold, confirm, cancel, expire (transactionally) |
| Draft explanatory text | Word every statement about a booking, from committed rows |

Problem statement: [problem-definition.md](problem-definition.md). Original brief and data package: `Archieve/` (the brief is a PDF; the
data guide is `Archieve/SetuHaul_Data_Package/setuhaul_database_guide.md`).

## 2. The supplied data and its limits

The data is one SQLite snapshot of **4 August 2026 (Asia/Kolkata)**, copied to `data/setuhaul.sqlite3` and hash-pinned: tests assert it
is never written. It is small: 4 carriers, 15 drivers, 15 vehicles, 2 facilities, 9 docks, 21 shipments, 106 appointment slots, 20
appointments, 12 ETA updates, 3 dock status events, 5 gate check-ins, 12 chat threads.

What it does **not** contain, and the consequences (details in [snapshot-limitations.md](snapshot-limitations.md) and
[data-audit.md](data-audit.md)):

* **No labour or consignee data.** Without crew availability and customer delivery commitments the service cannot be sure a slot is workable.
  Rule: *missing evidence is never assumed*, so every slot is "cannot confirm availability" and goes to a human. The demo console
  adds clearly labelled `DEMO_FIXTURE` rows so the flow can be shown; production needs a real feed (open question Q17).
* **No GPS at the time of the brief.** Added later as a supporting signal (section 9).
* **Awkward loads:** SHP1009 and SHP1014 need 75-minute unloads but slots are 60 minutes; the rule is strict (must fit one slot), so they hand off (Q20).
* It is one day and 21 shipments. It supports no claim about operational success at scale.

## 3. System at a glance

```
                      +------------------ driver chat channel (Telegram; signed HTTP channel)
                      |
driver message --> api.py  (signature, validation, duplicate check, rate limit, claim message id)
                      |
                conversation.py  (save text, detect language, build prompt, run model loop, finalise reply)
                      |
              model_gateway.py  (LangGraph loop: max 6 rounds, 4 tools, 30 s, cost cap; provider adapters)
                      |
                  tools.py   (allowed now? valid args? quote in THIS message? one write per message?)
                      |
   eta.py  feasibility.py  option_history.py  booking.py  cancellation.py  escalation.py   <- deterministic, transactional
                      |
                SQLite (system of record)  +  Redis (cache only)
                      |
   notifications outbox --> planner / consignee / operations        coordinators work cases via API or CLI
```

Technology: Python 3.12, FastAPI + uvicorn, Pydantic v2, SQLite (WAL), Redis (cache and rate limit; `fakeredis` in tests), LangGraph (model loop),
httpx (provider calls), pytest + Hypothesis, ruff, mypy `strict`. Pinned in `pyproject.toml`; full lock in `docs/requirements-lock.txt`.

## 4. Five rules that explain most of the code

1. **The model never supplies identity.** No tool has a `driver_id` or `thread_id` argument. The driver comes from the verified phone number (or a server-made session). An invented field is rejected (`extra="forbid"`).
2. **Time is injected.** Nothing calls `datetime.now()`. Code takes a `Clock`; tests and the demo use a `FakeClock` so a 180-second hold, a 20-per-minute limit and a 30-day purge are testable without waiting.
3. **The database is the referee.** A hold is an `appointments` row with status `HELD`, covered by the same unique index as a booking. Two drivers, a planner's tool, or a bug in our code collide on the database constraint.
4. **Facts come from committed rows.** "Held", "confirmed", "cancelled" and hand-off wording are rendered from stored state in three languages. If model text claims something the database does not back, it is replaced.
5. **Missing evidence is never assumed.** No labour data means "cannot check", never "assume free".

If you remember only these, you can predict how almost any new feature should be built.

## 5. Repository map

```
src/setuhaul/    the service (layers in section 6; auth/ holds sign-in, roles, Telegram and the web screens)
migrations/      001-022, numbered SQL applied in order to working copies
tests/           about 1,970 tests; tests/traces/ = recorded conversations; tests/scenario_flow.py = the end-to-end scenario
scripts/         evidence and demo scripts (audit, replay, scenario, show_phaseN, integrity proof, scale profile, run_demo)
load/            Locust profile
docs/            decisions, readout, verification evidence, this document, walkthrough, scenario catalogue
data/            setuhaul.sqlite3 (pristine seed, never written)
Archieve/        the brief, the original data package and an earlier flat prototype (not part of this build)
.env.example     the environment variables the service reads (the real .env is git-ignored)
```

Order to read the code: `clock`, `config`, `db`, `schemas` (foundations) then `identity`, `memory`, then `eta`, `feasibility`,
`booking`, then `tools`, `conversation`, `api`. `docs/code-walkthrough/01-08` follows the same order.

## 6. Layer by layer

Each part states the **purpose**, the **key behaviour**, and **what to look at first**. Test names are in the per-layer walkthroughs and the
[scenario catalogue](scenario-catalogue.md).

### 6.1 Foundations: `clock.py`, `config.py`, `db.py`, `schemas.py`, `migrations/`

* `Clock` is a one-method protocol; `SystemClock` returns real IST; `FakeClock` is moved by tests and the demo and rejects naive datetimes.
* `Settings` is a frozen dataclass of policy: hold lifetime 180 s, memory 20 turns / 12,000 chars / 24 h, model caps (6 rounds, 4 tools, 8 s per call, 30 s per message),
  rate limit 20/minute and 100/hour, cancellation cut-off 120 minutes, option lifetime 15 minutes, state freshness 5 minutes, GPS values (section 9). Only the database path, timezone and Redis URL come from the environment; policy changes are code reviews, not environment tweaks. `escalation_sla_minutes` is `None`: no response time is promised because none is approved.
* `db.py`: `parse_operational_timestamp` accepts only ISO-8601 **with an offset**; naive strings are rejected. `open_connection` sets WAL, foreign keys, 5 s busy timeout. `apply_migrations` runs each file in a transaction. `make_working_db` copies the seed and migrates the copy. `immediate()` opens `BEGIN IMMEDIATE`, taking the write lock up front so racing requests queue; any exception rolls everything back.
* `schemas.py`: Pydantic contracts. `DelayReport` has three kinds (`LATE_BY`, `REPAIR`, `EXPLICIT_ETA`); `EtaEvidence`, `ClarificationNeeded` and `EtaConflict` are the three outcomes of ETA derivation.

Migrations (each file is commented line by line):

| # | Adds | Key idea |
|---|---|---|
| 001 | option sets | exactly what each driver was shown |
| 002 | `HELD` status, hold metadata | **the capacity lock**: one active row per slot, holds included |
| 003 | history, notification outbox, maintenance runs | immutable history (triggers abort UPDATE/DELETE); outbox with unique dedupe key |
| 004 | escalation cases and events | one active case per key |
| 005 | identities, sessions, conversation state | session tokens stored only as a hash |
| 006 | labour windows, consignee commitments | empty by default, so feasibility says "cannot check" |
| 007 | idempotency keys | exact-retry answers |
| 008 | inbound messages | duplicate webhook handling |
| 009 | GPS pings, facility coordinates | supporting ETA signal |
| 010 | partner warehouse requests | book only after an approval reply |
| 011, 012 | users, sessions, onboarding, projects, Telegram links, audit | invite-only web sign-in, append-only audit |
| 013 | facility change requests | the destination approves |
| 014 | model calls and turns | numbers and codes only |
| 015 | saved settings | keys encrypted |
| 016, 017 | shipment assignments, Dock Access Pass tables, `SECURITY` role | the pass, not an order number, authorises a visit |
| 018, 019 | dock blocks, driver deletions | warehouse exceptions; nothing is erased |
| 020 | one-time account links | email and password sign-in |
| 021 | which load each chat message is about | what a warehouse user may read |
| 022 | crew number per warehouse | slots are created only where staff entered one |

### 6.2 Identity, memory, limits: `identity.py`, `memory.py`, `rate_limit.py`, `observability.py`

* Identity: HMAC-SHA256 over the **raw bytes before parsing** (constant-time compare); phone normalised to E.164; `UNKNOWN_PHONE` and `DRIVER_NOT_ALLOWED` refusals; session tokens random 32 bytes, only the SHA-256 stored; a shipment that is missing and a shipment that belongs to someone else give the **same** error so ids cannot be probed.
* Memory: **SQL is the truth, Redis is a cache.** Every message is committed to `chat_messages` first; the cache is a bounded window keyed by driver *and* thread. Lose Redis and `load` rebuilds from SQL. Conversation state (selected shipment, latest menu, hold) is never trimmed. A remembered hold is **re-verified against the database on every load**; if it lapsed, the driver is told once.
* Rate limit: fixed windows in Redis; denied messages are rolled back so refusals do not use up budget. (Increment/rollback is not atomic across processes; stated, accepted for an abuse limit.)
* Observability: request ids, spans with reason codes, `redact` masks secrets and phone numbers before logging.

### 6.3 ETA: `eta.py`

`derive_eta` returns exactly one of `EtaEvidence`, `ClarificationNeeded`, `EtaConflict`. Rules in order:

1. Gate check-in exists and no delay report: the gate time is the arrival (high confidence). With a delay report it is a **contradiction** (`CONTRADICTORY_ARRIVAL_STATE`) and a human decides.
2. No report: ask how late.
3. Explicit time: used as declared.
4. Repair: needs travel time; without it `MISSING_TRAVEL_TIME` (or, with a fresh GPS ping, the GPS estimate fills it, section 9). ETA = start + repair + drive.
5. "N minutes late": counted from the **planned** ETA (decision D6) unless the driver names the current ETA as the baseline.

See it: `python scripts/show_eta_cases.py` prints each case as JSON.

### 6.4 Capacity and feasibility: `capacity.py`, `feasibility.py`, `allocation.py`, `option_history.py`

* `build_facility_state` loads docks, slots, dock events, occupancy and labour into memory so slot evaluation is pure. **Expired holds do not occupy capacity**, whether or not the sweep has run. `slot_version` hashes every fact that decides offerability; if it changes between "shown" and "hold requested", the option is stale.
* `evaluate_slot` returns `FEASIBLE`, `INFEASIBLE` (all failing reasons listed), `REQUIRES_APPROVAL` (deadline missed with a penalty, or an exceptional customer) or `INCOMPLETE_FEASIBILITY_DATA` (no labour/customer data, or snapshot older than 5 minutes). Hard failures outrank missing data. Hard reasons include slot in the past, blocked, dock out of service, outside hours, dock-type or weight mismatch, refrigeration, unload longer than the slot, truck cannot arrive in time.
* Ranking: feasible slots only, sorted by start, dock code, slot id (so ties never vary), first three kept; the first is held. Policy D5: **first come first served at successful hold commit**; priority never pre-empts an existing hold.
* Option history: the options are stored with their fingerprints and live 15 minutes. `diff_option_set` re-judges each and explains changes (`NEWLY_OCCUPIED`, `NEWLY_HELD`, ...). The driver sees one held slot at a time (D93).

### 6.5 Holds, confirmation, cancellation: `booking.py`, `cancellation.py`, `events.py`, `notifications.py`, `integrity.py`, `jobs/`

This is the **transactional core**. Read `booking.py` closely.

* `acquire_hold` (one `BEGIN IMMEDIATE`): load the stored option set (proves ownership), pick the rank, lazily clear overdue holds on this shipment/slot, reject option sets older than 15 minutes, return the existing hold if the same slot is requested again, `SHIPMENT_ALREADY_HELD` for a different slot, **re-judge the option against the facility now** (`SLOT_CONFLICT` if someone else took it, `OPTION_CHANGED` otherwise), insert the `HELD` row and metadata, append history. `IntegrityError` from the unique index means a stale read lost a race: `SLOT_CONFLICT`. A busy database is `LOCK_TIMEOUT`, **never** reported as a conflict or a success.
* `confirm_reschedule` (one transaction, all or nothing): idempotency check, ownership, expiry, **feasibility re-evaluated**, original not already `IN_PROGRESS`, then end the original, promote the hold, append history, queue three notices, store the idempotent answer. A fault at any named step rolls everything back and the original appointment stays.
* `cancellation.py`: "never mind" releases the hold only. Cancelling a confirmed appointment takes two messages (the system asks, the driver replies Yes) and is automatic only if not in progress, more than 120 minutes out, and a known penalty-free customer commitment; otherwise it **escalates and changes nothing**. Returning to the original slot is a new request, re-judged, never restored.
* Outbox: notices are queued inside the booking transaction and delivered outside it (`deliver_pending_notifications`), at least once with a stable `dedupe_key`; delivery failure never touches the appointment; transient failures retry up to 3 times.
* Expiry: `expire_due_holds` is inclusive (`expires <= now`), batched, idempotent. `MaintenanceRunner` runs it every 30 s in a background thread.
* `integrity.check_invariants` runs 12 queries that must return no rows (no duplicate active slot, one hold per shipment, metadata matches state, history has parents, ...). The concurrency proof runs it after every action.

### 6.6 The model boundary: `model_gateway.py`, `tools.py`, `prompts.py`, `language.py`, `conversation.py`, `stub_model.py`

* Gateways: `AnthropicAdapter`, `OpenAIAdapter` (httpx), `ScriptedGateway` (tests, replay), `StubGateway` (rule-based stand-in). `complete_with_retry` retries once on transient errors only; final failure is `ModelUnavailable` and the caller promises nothing. `run_bounded_loop` is a two-node LangGraph (`model`, `tools`) whose **caps are enforced in node code**, not by the recursion limit. No model call ever happens inside a database transaction.
* Tools: read (`list_my_shipments`, `get_shipment_status`, `compute_eta`, `search_slot_options`, `compare_option_set`, `offer_original_slot`), write (`request_hold`, `confirm_reschedule`, `release_pending_change`, `cancel_appointment`), and `escalate_to_operations`. `ToolRegistry.execute` runs five checkpoints in order: **allowed in this state?** (`TOOL_NOT_ALLOWED`) then **valid arguments?** (`INVALID_ARGUMENTS`) then, for writes, **is the `driver_quote` inside the driver's current message?** (`APPROVAL_NOT_PRESENT`) then **at most one write per message** (`ONE_WRITE_PER_MESSAGE`) then the handler. Confirm needs a hold created in an *earlier* message; the model cannot hold and confirm in one breath.
* Prompt: written as boundaries; contains a per-process **canary** string; if the canary or a substantial prompt line appears in a reply, the reply is blocked. Untrusted text (load notes) is passed only under `untrusted_text` and marked as data.
* Language: English, Hindi (Devanagari) and Hinglish. `detect_language` counts script letters and marker words. `T` is the wording table for every system-stated fact. `claims_confirmed` / `claims_held` catch model text that claims a booking.
* `conversation.handle_message` runs a turn: save text, language, system facts into the prompt, bounded loop, then `_finalize`: committed hold/confirm/cancel/release gets **system wording from the row**; a hand-off gets the case reference; an offer is worded from the held row; otherwise model text passes through `_guard`, which replaces prompt leaks and unbacked claims. Any database or service failure inside a turn becomes a safe reply plus a hand-off recorded in a fallback file.

### 6.7 Escalation and the HTTP API: `escalation.py`, `coordinator.py`, `api.py`

* A hand-off is a **case** in a queue: `OPEN` to `CLAIMED` to `RESOLVED` (or `CANCELLED` by the driver); resolved cases can be reopened within 24 hours; every transition is an immutable event. A dedupe key (driver, thread, shipment, reason) folds repeats into the active case; a partial unique index enforces it. Two coordinators racing produce one owner. The driver is told in chat, and on their phone when the case names a load, when it is claimed or resolved; resolving with `ASSIGNED_SLOT` from the web books the chosen slot. Resolution codes: `ASSIGNED_SLOT`, `ADVISED_DRIVER`, `CANCELLED_BY_OPERATIONS`, `NO_ACTION_NEEDED`, `OTHER`. If the database is down, `JsonlEscalationSink` appends to a file and `ingest_fallback_file` loads it later, once.
* `api.py` order of work on a driver message, cheapest and safest first, **always before the model**: signature (401) then validation, text up to 2000 characters (422) then sender lookup (403) then duplicate check (same text gets the stored reply with `duplicate: true`; same id with different text is 409; still-processing is 409 unless the claim is over 60 s old) then rate limit (429 with `Retry-After`) then claim the id, run the conversation, store the reply, return it. A failure frees the id so a retry works. Errors are always `{status, reason_code, request_id}`; no stack trace or SQL leaves the process.

| Route | Purpose |
|---|---|
| `POST /v1/driver/messages` | the driver message webhook |
| `GET /v1/driver/status` | signed, read-only |
| `POST /v1/gps/pings` | signed telematics ping, names the shipment (section 9) |
| `POST /v1/driver/location` | signed driver-shared location; the driver is found from `from` (section 9) |
| `GET /health`, `GET /ready`, `GET /health/ready`, `GET /version` | liveness; database and cache usable; deployed commit |
| `GET/POST /v1/coordinator/cases...` | list, show, claim, resolve, reopen (bearer token) |

`python -m setuhaul.coordinator --db data/working.sqlite3 list|show|claim|resolve|reopen` is the CLI over the same queue.

### 6.7a Web screens and warehouse operations: `auth/`, `assign.py`, `release.py`, `dock_blocks.py`, `round_clock.py`, `driver_messages.py`, `passes.py`, `gate.py`

* `auth/` holds sign-in (Google, email and password, break-glass), roles and facility scope, the server-rendered screens (`/warehouse`,
  `/admin`, `/gate`, `/wiki`), the Telegram webhook and their JSON routes.
* Shipments created on **Warehouse > Shipments** are hidden from the driver until they send back their Dock Access Pass
  (`passes.py`, `pass_check.py`); the gate checks it (`gate.py`). See [dock-access-pass.md](dock-access-pass.md).
* A person can change a booking in three ways, each one transaction: resolving a case with **Assigned a slot** (`assign.py`, judged by
  the same `evaluate_slot` rules), blocking a dock (`dock_blocks.py`, which moves or frees the bookings it showed you) and freeing one
  slot (`release.py`).
* `round_clock.py` opens every warehouse 24 hours at start-up and keeps 14 days of one-hour slots for warehouses with a crew number.
* `driver_messages.py`: Operations writes to a driver and resends a failed message.
* The screens' usage text is in `auth/wiki_pages.py`; change it with the screen.

### 6.8 Evaluation and tracing: `evaluation.py`, `langsmith_trace.py`, `proof.py`, `scale_fixture.py`

* A `Trace` records turns, model rounds and tool events; **live** traces come from a real model, **scripted** ones are regression baselines (they say nothing about model quality). `sanitize_trace` replaces ids and masks phones/emails/secrets.
* Evaluators are plain functions over a trace: `identity`, `tool_order`, `explicit_approval`, `option_set_ownership`, `state_backed_wording`, `round_caps`, `handoff_complete`.
* **Replay** feeds recorded model outputs back through the real deterministic layers in a fresh database and compares reply, status and tool results. A mismatch means a rule changed. No network, no model. `python scripts/replay_traces.py`.
* LangSmith tracing is off unless `SETUHAUL_LANGSMITH_ENABLED=true` (default false), `SETUHAUL_LANGSMITH=approved` and a key are all set; everything is scrubbed first.
* `proof.py` and `scale_fixture.py` back the concurrency proof (threads and processes racing for one slot) and the generated 360-load scale profile (`GEN-` ids; not SetuHaul data).

## 7. One message, end to end (the "happy path")

Driver Manoj Sharma (shipment SHP1006, planned ETA 10:20) writes at 09:35: *"my truck is 60 minutes late"*.

1. **api.py** verifies the signature, finds Manoj by phone, checks it is not a duplicate, passes the rate limit, claims the message id.
2. **conversation.py** saves the text, detects English, builds the prompt with system facts (active shipments, no hold yet) and starts the model loop.
3. The model calls **`search_slot_options`** with a `LATE_BY 60` report. `tools.py` allows it, validates it, and the handler runs `derive_eta` (10:20 + 60 = 11:20) then `search_feasible_slots` over live capacity.
4. Three slots come back; `option_history` stores them (valid until 09:50) with fingerprints and the first is **held** (D2 12:00).
5. The system words the offer: "I am holding 12:00-13:00 at dock D2 ... until 09:38. It is not booked yet. Reply Yes to confirm, or No for the next slot."
6. Driver replies *"no"* to release it and hold the next, or *"yes"* to book it.
7. Driver replies *"yes, confirm it"* in a **later message**. `confirm_reschedule` re-evaluates feasibility, ends the original (10:00 at D4), promotes the hold, writes history, queues three notices, all in one transaction. Reply: "Your appointment is confirmed ... Reference APT-H-...".
8. `deliver_pending_notifications` sends the planner, consignee and operations notices outside the transaction.

If the driver waits more than 3 minutes, the sweep expires the hold, the next message announces the lapse once, and **the original appointment still stands**.

## 8. Failure model: what happens when things go wrong

| Failure | Behaviour |
|---|---|
| Two requests want one slot | the unique index lets exactly one in; the other gets `SLOT_CONFLICT` |
| Hold lapses before confirm | refused; original unchanged; driver told once |
| Slot changed since offered | `OPTION_CHANGED` or `SLOT_CONFLICT`, explained |
| Model unreachable / times out twice | "cannot check bookings right now", a `MODEL_UNAVAILABLE` case, nothing promised |
| Model claims "confirmed" with nothing committed | text replaced by a safe sentence |
| Model leaks the prompt (even partly) | reply blocked |
| Model invents `driver_id` or a quote | `INVALID_ARGUMENTS` / `APPROVAL_NOT_PRESENT`, no write |
| Database down mid-turn | safe reply, hand-off written to a fallback file, ingested later |
| Redis lost | rebuilt from SQL |
| Duplicate webhook delivery | stored reply returned, no second model call |
| Notification delivery fails | appointment untouched; retried (outbox) |
| No slot fits / contradictory data / missing evidence | hand-off case with context; booking unchanged |

Each row has a named test; see the [scenario catalogue](scenario-catalogue.md).

## 9. The GPS ETA feature (latest change)

Design: [superpowers/specs/2026-11-21-gps-eta-design.md](superpowers/specs/2026-11-21-gps-eta-design.md); plan: [superpowers/plans/2026-11-21-gps-eta.md](superpowers/plans/2026-11-21-gps-eta.md).

* **Role:** a *supporting signal*, never authoritative. A stopped, broken-down truck still has a position, so GPS cannot override the driver. Gate check-in (a fact) still outranks everything.
* **Storage:** migration 009 adds `gps_pings` (lat/lon with database CHECK ranges, `recorded_at`, `received_at`, `source`) and facility `lat`/`lon`. `gps.py`: `store_ping`, `load_latest_ping` (ignores pings older than 15 minutes and treats a future-stamped ping as age zero), `gps_travel_minutes_for`.
* **Estimate:** `travel.py`: haversine distance x road factor 1.3 / 40 km/h, rounded up. Builder defaults in `config.py` (`gps_avg_speed_kmph`, `gps_road_factor`), swappable behind a `TravelTimeEstimator` protocol.
* **Two effects in `derive_eta`** (new optional `gps_travel_minutes` argument; omit it and behaviour is exactly as before):
  1. `REPAIR` with no driver drive time: use the GPS travel time. Source `GPS_ASSISTED`, confidence capped at `MEDIUM`.
  2. `LATE_BY` / `EXPLICIT_ETA`: if the driver's ETA and the GPS ETA differ by more than 30 minutes (`gps_mismatch_minutes`; exactly 30 is not a discrepancy), keep the driver's ETA, lower confidence one level, set `gps_discrepancy_minutes`.
* **Surfacing:** `tools._eta_for` loads the ping and passes it in; the driver's reply gets a note ("GPS suggests your arrival differs from your report by about N minutes"); the hand-off summary carries it. A normal offer opens no case, so the coordinator sees the discrepancy only when a search ends in a hand-off.
* **Entry points** (both signed like the webhook; both reject bad coordinates and pings stamped more than 5 minutes in the future): `POST /v1/gps/pings` for a telematics sender that names the shipment, and `POST /v1/driver/location` for a driver-shared location, where the driver and their active shipment come from the sender's phone. Both were exercised on 2026-11-21 (see the running guide). The first production source intended is driver-shared location in the chat channel; a real messaging-channel adapter and fleet telematics are not built.
* **Demo caveat:** the console's **Send ping** calls `store_ping` directly and skips the endpoint's signature, driver lookup and future-time guard; it also deletes pings stamped later than the one sent so a back-dated test ping becomes the latest.

## 10. Configuration, environments and running modes

| Mode | How | Model |
|---|---|---|
| Tests | `python -m pytest -q` | scripted/mock only |
| Demo console | `python scripts/run_demo.py` | the model configured in `.env`; with none, the chat is switched off (no stand-in) |
| Service | `python -m uvicorn --factory setuhaul.api:create_app_from_env --port 8000` | none configured means every turn takes the safe "cannot check" path and opens a case |
| Load tests | `SETUHAUL_USE_STUB_MODEL=1`, `fakeredis://`, `SETUHAUL_FAKE_NOW` | stub (never in production) |

Settings an admin saves on **Admin > Configuration** (model, LangSmith, Telegram, email) override the environment.
Environment variables (from `.env.example`): `SETUHAUL_DB_PATH`, `SETUHAUL_TIMEZONE`, `SETUHAUL_REDIS_URL`, `SETUHAUL_WEBHOOK_SECRET`,
`SETUHAUL_COORDINATOR_TOKENS` (JSON token to coordinator name), `SETUHAUL_MODEL_PROVIDER` (`openai` or `anthropic`), `SETUHAUL_MODEL_ID`,
`SETUHAUL_MODEL_API_KEY`, optional price variables for the cost stop, OpenAI extras, the LangSmith switches, the sign-in, mail and Telegram variables (see [running-and-demonstrating.md](running-and-demonstrating.md), section 7a). **No secret lives in the repo.**
Make a writable database with `make_working_db(Path('data/working.sqlite3'))`; never run against `data/setuhaul.sqlite3` directly.

## 11. Quality gates and evidence

| Gate | Command | Result on 2026-11-27 |
|---|---|---|
| Tests | `python -m pytest -q` | 1,972 passed in about 3 minutes |
| Lint | `python -m ruff check .` | all checks passed |
| Types | `python -m mypy src` | no issues in 105 source files (`strict`) |
| End-to-end scenario | `python scripts/run_scenario.py` | **23/23 checks** |
| Trace replay | `python scripts/replay_traces.py` | **9/9 traces clean, 109/109 evaluator checks**; live recordings of the earlier menu flow are retired |
| Concurrency | `python scripts/run_integrity_proof.py` (about 8 min, not re-run) | recorded in `docs/verification/concurrency-proof.md` |

Evidence files for every phase are in `docs/verification/`. The client-facing summary is [client-readout.md](client-readout.md).

**Strong claims:** no double allocation (20 rounds of thread, process and mixed contention, zero invariant violations, for SQLite on one
machine); wording matches stored state on the deterministic path; failure paths safe; capacity search p95 about 13 ms.
**Weak or absent claims:** real-model behaviour on the current flow, Hindi/Hinglish quality, auto-resolution rate, multi-worker behaviour, real Redis.

## 12. Decisions, open questions and risks

* [decision-register.md](decision-register.md) lists decisions D1 onward with the reason for each (examples: D5 FCFS, D6 delay counted from planned ETA, D13 LangGraph, D64 demo console, D93 one slot held instead of a menu, D101 Dock Access Pass).
* [operations-questions.md](operations-questions.md) lists Q1-Q20, each an **engineering default awaiting an approver**. Blockers: model provider and data protection (Q9), labour/consignee data source (Q17), cancellation cut-off (Q6), ETA buffer and priority classes (Q3, Q4), unload longer than a slot (Q20).
* Top risks from the readout: live-model evidence is thin (a handful of runs, English); Hindi/Hinglish wording is unreviewed by a native speaker and language detection and the claim guard are heuristics; no labour source means every case reaches a human; the messaging-provider integration and coordinator authentication are minimal; SQLite saturates around 245 writes/s (move to PostgreSQL before more than one sustained writer or more than four API workers; the invariants carry over).

## 13. Known gaps (read before you trust a claim)

* No recorded real-model run of the current one-slot flow. The Telegram trial used the stand-in.
* No data retention, export or erasure for personal data; no encryption at rest; no privacy notice. See the [FAQ](faq.md), section 6.
* Notices for warehouse, customer and operations are queued; no sender is connected.
* Telegram: photos, voice notes and locations are not read; the driver is told so.
* No deployment has been run through the ForgeCI set-up ([deployment-hostinger.md](deployment-hostinger.md)).
* The rule-based stand-in understands only set phrases.
* `scripts/show_phase8.py` needs `PYTHONUTF8=1` on Windows to print Hindi.
* The scripted scenario run rewrites `docs/verification/scenario-run.md`; restore it with `git checkout` if you only wanted to look.
* A new working database needs `setuhaul.identity.provision_identities(conn)` before phone-based messages work (running guide, section 7).

## 14. Onboarding path and exercises

**Day 1 (understand).** Read sections 1, 4 and 7 here. Run the demo console and do scenario A and B ([running guide](running-and-demonstrating.md)). Open `booking.py` and find the three places the unique index is the real protection.

**Day 2 (trace).** Set a breakpoint or add a print in `tools.ToolRegistry.execute` and follow the happy path. Read `conversation._finalize`. Read `docs/verification/scenario-run.md`.

**Day 3 (change).** Pick one and keep every gate green:

1. Change the hold lifetime to 240 seconds. Which tests fail and why? (They encode the old value; fix the tests, not the rule.)
2. Add a new hand-off reason code. Follow it through `escalation.py`, the wording table in `language.py`, the demo console label map, and the catalogue.
3. Add a facility-rule check to `feasibility.py` (for example a maximum number of arrivals per hour). Add a test first; check that missing data still yields `INCOMPLETE_FEASIBILITY_DATA`, not `FEASIBLE`.
4. Add a new tool. Notice that you cannot add a `driver_id` argument and why.

**Rules for changes.** Add a migration, never edit one that has run. No `datetime.now()`. No model call inside a transaction. Wording that states a booking fact goes in `language.py` and is rendered from rows. Any new write needs a test that a fault at each step rolls back. Run `pytest`, `ruff check .`, `mypy src` before committing; `.pre-commit-config.yaml` and `scripts/ci.py` run the same checks locally; GitHub Actions is not used (D56).

## 15. Glossary

| Term | Meaning |
|---|---|
| Slot | one time window on one dock |
| Appointment | a shipment's booking of a slot (`CONFIRMED`, `IN_PROGRESS`, `CANCELLED`, ...) |
| Hold | an appointment row with status `HELD`, 3 minutes, same unique index as a booking |
| Option set | up to three feasible slots stored for one driver, valid 15 minutes; one is held at a time |
| Hand-off / escalation / case | a queued task for a coordinator, with full context |
| Outbox | notices queued in the booking transaction and delivered afterwards |
| Idempotency | the same message id returns the same stored answer |
| Stub | the rule-based stand-in model for demos and load tests |
| Trace / replay | a recorded conversation re-run through the deterministic layers to detect rule changes |
| FCFS | first come, first served at successful hold commit |
| IST | Asia/Kolkata, the only timezone used |
