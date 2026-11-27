# Running the program and demonstrating its functionality

Companion to [Knowledge transfer](knowledge-transfer.md). This document says **what to run, what to type, and what you will see**.
> **Note:** the demo console chat uses the model configured in `.env`; with none, the chat is off. Where captured output below mentions
> a "stand-in" or "rule-based" assistant, read it as historical. Since 2026-11-25 the service holds one slot instead of listing
> three (section 11), so captured replies that show a numbered list are also historical.

Every output below was captured by running the program on 2026-11-21 (Windows, Python 3.12, rule-based assistant, no real model), except
where marked *not run*. Ids such as `APT-H-...` and `ESC-...` are random and differ on every run.

Use **synthetic data only**. The demo uses a copy of the supplied snapshot, a simulated clock (Tue 4 Aug 2026, 09:35 IST) and clearly labelled
`DEMO_FIXTURE` evidence. Nothing real is touched.

Contents: 1 Setup · 2 Quality gates · 3 The demo console · 4 Scenarios A to F (console) · 5 GPS scenarios · 6 Evidence scripts · 7 The real HTTP service ·
7a Sign-in, roles and the Telegram bot · 8 Live model · 9 Suggested 15-minute demo · 10 Troubleshooting

---

## 1. Setup (once)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
```

Everything below uses `.\.venv\Scripts\python` (written as `python` for brevity). On Linux or macOS use `.venv/bin/python`.
Set `PYTHONUTF8=1` (PowerShell: `$env:PYTHONUTF8 = "1"`) before any script that prints Hindi; the default Windows console encoding crashes on Devanagari.

## 2. Quality gates (about 1 minute)

```powershell
python -m pytest -q
python -m ruff check .
python -m mypy src
```

Observed on 2026-11-26: `1364 passed`, `All checks passed!`, `Success: no issues found in 71 source files`.

## 3. The demo console

```powershell
python scripts/run_demo.py            # opens http://127.0.0.1:8800/ : sign in, then go to /demo
python scripts/run_demo.py --auto-login   # NO authentication: /demo/login signs you in as a demo warehouse user
python scripts/run_demo.py --no-browser --port 8800   # headless
python scripts/run_demo.py --fresh    # start with an empty dock board (no existing appointments)
```

**The demo uses the real sign-in.** `run_demo.py` reads these from `.env` (or the environment) and shows a login page at `/`; signed-out
visitors to `/demo` are sent there. Only what is configured is offered.

* **Backup admin password** (works without Google): set `SETUHAUL_BOOTSTRAP_ADMIN_EMAIL=<you>` and `SETUHAUL_LOCAL_ADMIN=1`. The demo
  asks for the password in the terminal at each start (12 characters or more); it is not stored, because the demo database is rebuilt.
* **Google**: also set `SETUHAUL_GOOGLE_CLIENT_ID` and `SETUHAUL_GOOGLE_CLIENT_SECRET`, and register
  `http://127.0.0.1:8800/auth/callback` as a redirect URI on your OAuth client. Open the page as `127.0.0.1`, the same host. The signing
  secret is generated for the run if `SETUHAUL_SESSION_SECRET` is unset. `SETUHAUL_BOOTSTRAP_ADMIN_EMAIL` must be your Google address.
* `--auto-login` restores the old shortcut (`/demo/login`, loopback only) and **skips authentication**.
* The demo clock is frozen, so a session does not expire and a locked backup password does not unlock until the demo is restarted.

| Screen | What it is | As the demo user |
|---|---|---|
| `/` | sign-in page; a signed-in user is sent on to their screen | redirects to `/warehouse`. The page offers only the sign-in methods you configured |
| `/warehouse` | dock board, cases, partner requests, driver chats, projects, drivers | works |
| `/admin` | tabs: users, pending drivers, onboarded drivers, audit log, model | "Not allowed" (403): admins only |

The demo has no Telegram and no Google sign-in. To see the admin screen, run the real service (section 7a).

`--fresh` deletes the supplied appointments, so every slot is free and no load has a booking; drivers, loads, evidence and the clock are unchanged.
You can also switch at any time: **Reset demo** offers **Supplied schedule** or **Empty dock board**. Scenarios A to F below use the supplied schedule;
the empty-board walkthrough (observed output, two drivers competing, a hand-off that still happens) is scenario G in the
[demonstration guide](demonstration-guide.md#g-fresh-run-an-empty-dock-board).

If a `.env` with a provider, model id and key exists, a **Live model** switch becomes usable; otherwise the assistant is the **rule-based
stand-in** (deterministic; it understands "N minutes late/delay", "first"/"take", "yes"/"confirm", "no" and "change facility"; it does **not** understand breakdown/repair wording, "keep my original slot" or "cancel"). It says nothing about model quality.
To guarantee the stand-in, start with `$env:SETUHAUL_DOTENV = "off"` first.

The page has three panels and a top bar:

* **Driver console** (left): pick a driver, see their loads, GPS readout and conversation. **Read only** for every web user (decision D90):
  no message box, quick replies or ping button, and the server refuses those requests (`403 READ_ONLY_CONSOLE`, audited). Drivers write from Telegram.
  Walkthrough steps below that type as the driver can no longer be done in the console. The local demo has no Telegram connection, so its
  conversations stay empty until someone uses the demo driver account below.
* **Demo driver account** (separate page, decision D91): open `http://127.0.0.1:8800/demo/driver-login` (default driver Manoj Sharma,
  `?driver=DRV007` for another). It sends messages as that one driver and shows only that driver's conversation: no dock board, no
  operations. It has its own cookie, so you can keep the admin console open in another tab and watch. Local demo and loopback only.
* **Dock board** (middle): docks by hour for one facility; Available, Held (amber, 3 min), Booked, Blocked, and the selected driver's own cells.
* **Operations** (right): **Cases** (the hand-off queue) and **Activity** (appointment history, notification outbox, GPS pings).
* **Top bar:** the model name, the simulated clock, **+1 min**, **+3 min and sweep**, **Reset demo**.

**Reset demo** shows a confirmation dialog; click **Reset** in it. It rebuilds the schedule, clears chats and cases and rewinds the clock to 09:35.
Do this between scenarios. Each chat message advances the clock 20 seconds, so a 180-second hold can be watched lapsing.

Direct API calls to the same routes (`/demo/api/chat`, `/clock`, `/board`, `/cases`, `/gps`, ...) are used below to show exact text; they run the same code as the page.
A script calling them must first keep the session cookie from a sign-in (`/demo/login` sets one when started with `--auto-login`) (for example one `httpx.Client` for all calls).

> **Changed 2026-11-25 (D93):** the numbered menu is gone. A delay report (or a closer truck) **holds one slot**; the driver replies
> **Yes** to book it or **No** for the next one. Where a step below picks a number or shows three options, see section 11; the
> screenshots and recorded outputs below predate this change.

## 4. Scenarios on the console

The current step-by-step scenarios, with the replies to expect, are in the [demonstration guide](demonstration-guide.md), section 2:
happy path, lapsed hold, held capacity, hand-off, change of warehouse, keep or cancel, languages and an empty board.

In short: a delay report holds one slot, for example *I am holding 12:00-13:00 at dock D2 on 04 Aug 2026, SetuHaul Jaipur Distribution Centre, for you until 09:38. It is not booked
yet. Reply Yes to confirm, or No for the next slot.* The driver replies `yes` to book it or `no` for the next one.

---

## 5. GPS scenarios (new)

The console gives each driver a **Truck position** panel: latitude, longitude, a distance-from-dock slider and **Ping age (min)**. Nothing is stored until **Send ping**.
Each demo driver starts with a seeded ping 60 km north of their dock (readout: `60 km from SetuHaul Jaipur Distribution Centre, ping 0 min old, fresh. Drive time about 117 min, GPS ETA 11:32`),
using road factor 1.3 at 40 km/h.

| # | Do | Observed |
|---|---|---|
| 1 Cross-check | Manoj; Send ping at 120 km out (lat 27.9915, lon 75.7873, age 0); message `my truck is 20 minutes late` | Readout: `distance 120.0 km, travel 234 min, GPS ETA 13:29`. The menu is shown as usual and ends with `Note: your truck's GPS suggests your arrival differs from your report by about 169 minutes.` The driver's ETA (10:40) is kept, confidence lowered. |
| 2 Fill the drive time | Manoj; ping 30 km out, age 0; breakdown, repair 45 minutes, no drive time | Readout: `30.0 km, travel 59 min, GPS ETA 10:34`. See below: this needs a live model or direct call, because the stand-in does not understand repair wording. |
| 3 Stale ping | same, but Ping age 20 | Readout: `travel_minutes: null, gps_eta: null, fresh: false`. The service ignores the ping and asks for the drive time as before. |

Scenarios 2 and 3 need the repair message to be understood. With the stand-in, `breakdown, repair will take 45 minutes` produced only `How late are you, and which shipment is it?`
(observed), so show the rule itself with the real `derive_eta` (output observed; shipment SHP1006, now 09:35):

```powershell
@'
import sys; sys.path.insert(0,"src")
from setuhaul.clock import FakeClock
from setuhaul.db import open_connection, parse_operational_timestamp as ts
from setuhaul.eta import derive_eta, load_eta_records
from setuhaul.schemas import DelayReport
clock = FakeClock(ts("2026-08-04T09:35:00+05:30"))
with open_connection(readonly=True) as c: planned, current = load_eta_records(c, "SHP1006")
for name, rep, g in [("repair 45, GPS 59 min away", DelayReport.repair(45), 59),
                     ("repair 45, no/stale GPS", DelayReport.repair(45), None),
                     ("20 late, GPS 234 min away", DelayReport.late_by(20), 234),
                     ("20 late, GPS 60 min away", DelayReport.late_by(20), 60)]:
    print(name, derive_eta(rep, planned, current, None, clock, gps_travel_minutes=g).model_dump_json())
'@ | python -
```

```
repair 45, GPS 59 min away : eta 11:19, source GPS_ASSISTED,  confidence MEDIUM, discrepancy null
repair 45, no/stale GPS    : ClarificationNeeded MISSING_TRAVEL_TIME "After the repair, how long will the drive to the facility take?"
20 late, GPS 234 min away  : eta 10:40, source DRIVER_DECLARED, confidence LOW,    discrepancy 169
20 late, GPS 60 min away   : eta 10:40, source DRIVER_DECLARED, confidence MEDIUM, discrepancy null  (gap under 30 minutes)
```

(Run from the repository root. The block above shows the printed fields in short form.) Key message: **GPS fills a gap or raises a flag; it never overrides the driver.**
The unit tests for the boundary (exactly 30 minutes is not a discrepancy, 31 is) are in `tests/test_eta.py`.
Remember the console's Send calls the shared store function directly; the signed endpoints are shown in section 7.

## 5b. Partner warehouse by email (new)
1. Start the console (`python scripts/run_demo.py`, signed in). Open **Operations > Partner mail**, press **Request partner slot (12:00)**.
2. The request email appears (to the demo partner's coordinator). Switch the dock board to **Partner Warehouse Jaipur**: the request shows under the board as *waiting*, not booked.
3. Press **Partner replies APPROVE**. The reply is read from the (simulated) inbox, the slot is booked and the board cell turns purple (partner warehouse). The driver (Ravi Partner-Demo) gets the update in the chat.
4. Try **DECLINE**, or move time on with **+3 min and sweep** past 2 hours to see the request expire; the original booking is never touched.
5. Real Gmail: set the `.env` block, run `python scripts/check_email_config.py`, set `SETUHAUL_PARTNER_EMAIL_MODE=live`, restart. The tab then shows real sent mail; reply from the partner mailbox and press **Check mailbox**.

## 6. Evidence scripts (each is a demonstration of a failure or rule)

| Show this | Command | What you will see (observed) |
|---|---|---|
| ETA rules | `python scripts/show_eta_cases.py` | explicit 12:15; "90 min late" from planned gives 11:50 but from a named current ETA gives 12:50; repair without drive time gives `MISSING_TRAVEL_TIME`; gate-in 09:20 with no report gives `GATE_CHECKIN`/HIGH; gate-in with "60 min late" gives `CONTRADICTORY_ARRIVAL_STATE` |
| Search, menus, change detection | `python scripts/show_phase4.py` | snapshot as supplied: `INCOMPLETE_FEASIBILITY_DATA` for all 37 slots (missing labour and consignee); with fixtures SHP1006 gets D2 12:00, D4 12:00, D1 13:00; SHP1014 and SHP1009 get `NO_SLOT` (`UNLOAD_EXCEEDS_SLOT`); after a planner takes option 1: `NEWLY_OCCUPIED` "Option 1 was booked by someone else", options 2 and 3 `UNCHANGED` |
| Double-booking protection | `python scripts/show_phase5.py` | prints the four partial unique indexes; two valid requests for one slot: one `HELD`, one `SLOT_CONFLICT`; an outside writer inserting into the held slot: `UNIQUE constraint failed: appointments.slot_id`; second slot for one shipment: `SHIPMENT_ALREADY_HELD`; expiry at t+179 s still `LIVE`, at t+180 s `EXPIRED`, repeat sweep 0 |
| Sign-in, limits, memory | `python scripts/show_phase3.py` | bad signature `INVALID_SIGNATURE`; unknown phone `UNKNOWN_PHONE`; a client-supplied driver id as a session `SESSION_INVALID`; someone else's shipment `FOREIGN_SHIPMENT`; a session after 31 minutes `SESSION_EXPIRED`; then the 20-per-minute limit and hold re-verification on return |
| Idempotency, rollback, cancel, outbox | `python scripts/show_phase6.py` | "never mind" releases the hold and the original stays `CONFIRMED`; a retry of a lost confirmation replays the same answer; the same message id with a different hold is `IDEMPOTENCY_MISMATCH`; a fault injected mid-confirmation rolls back |
| Injection, outages, unbacked claims | `$env:PYTHONUTF8="1"; python scripts/show_phase8.py` | tool contract table; injection: `confirm_reschedule` gives `TOOL_NOT_ALLOWED`, an invented `driver_id` gives `INVALID_ARGUMENTS`, someone else's shipment gives `FOREIGN_SHIPMENT`, appointments unchanged, prompt-leak reply replaced by `I cannot share that.`; Hinglish and Hindi conversations; model timing out twice gives `I cannot check or change bookings right now, so I cannot promise anything...` and a case; model text "Great news, your 12:00 appointment is confirmed!" with nothing committed becomes `I have not confirmed or held anything yet.` |
| Hand-off queue and CLI | `python scripts/show_phase9.py` | a real conversation hands off (SHP1014); the CLI queue lists the case; `asha` claims it, `ben` gets `ALREADY_CLAIMED (held by asha)` and cannot resolve it (`NOT_YOUR_CASE`); the driver sees the update |
| Whole flow, scored | `python scripts/run_scenario.py` | 23 of 23 checks pass; offer, hold, expiry, confirmation, cancellation, three hand-offs (`NO_SLOT`, `CONTRADICTORY_DATA`, `INCOMPLETE_FEASIBILITY_DATA`), claim/resolve, outbox (one transient delivery failure retried), `check_invariants() -> []`. **It rewrites `docs/verification/scenario-run.md`**; run `git checkout docs/verification/scenario-run.md` afterwards if you do not want the change |
| Replay recorded conversations | `python scripts/replay_traces.py` | `9/9 traces replayed clean; 123/123 evaluator checks passed` (5 scripted, 4 live-model traces) |
| Concurrency (about 8 min) | `python scripts/run_integrity_proof.py` | threads and processes racing for one slot, invariants after every step (*not re-run here*; see `docs/verification/concurrency-proof.md`) |
| Scale | `python scripts/run_scale_profile.py` | generated 360-load fixture, spike, burst (*not re-run here*; `docs/verification/scale-test.md`) |
| Snapshot quality | `python scripts/audit_data.py` | read-only JSON audit: row counts (106 slots, 20 appointments, ...), integrity and timestamp checks, one shipment trace |

Lookup table for "show me X": see [scenario-catalogue.md](scenario-catalogue.md); each row names the test that proves it (`python -m pytest -q -k <test_name>`).

## 7. The real HTTP service (not the demo)

This path needs a **migrated working copy with driver phones provisioned**; the seed is never written.

```powershell
$env:PYTHONUTF8 = "1"
@'
import sys; sys.path.insert(0,"src")
from pathlib import Path
from setuhaul.db import make_working_db, open_connection
from setuhaul.identity import provision_identities
p = Path("data/working.sqlite3"); make_working_db(p, Path("data/setuhaul.sqlite3"))
c = open_connection(path=p); print("provisioned", provision_identities(c)); c.commit(); c.close()
'@ | python -
```
Observed: `provisioned 15`. (`make_working_db` alone is not enough; without the provisioning every message returns `UNKNOWN_PHONE`.)

```powershell
$env:SETUHAUL_DB_PATH = "data/working.sqlite3"
$env:SETUHAUL_WEBHOOK_SECRET = "demo-secret"
$env:SETUHAUL_COORDINATOR_TOKENS = '{"tok123":"asha"}'
$env:SETUHAUL_REDIS_URL = "fakeredis://"            # demo only; use a real Redis URL otherwise
$env:SETUHAUL_USE_STUB_MODEL = "1"                  # demo only; omit to use a configured real model
$env:SETUHAUL_FAKE_NOW = "2026-08-04T09:35:00+05:30"  # demo only; the snapshot is dated
python -m uvicorn --factory setuhaul.api:create_app_from_env --port 8000
```

With no model configured and no stub, each turn takes the safe "cannot check bookings right now" path and opens a case.

Health: `GET /health` gives `{"status":"ok"}` (even if the database is gone); `GET /ready` gives `{"ready":true,...}` when database and cache work, else 503 with a reason.

Signed calls (body signed with HMAC-SHA256 over the **raw bytes**, header `x-signature`). Observed results against the pristine snapshot, driver Manoj Sharma:

| Request | HTTP | Result |
|---|---|---|
| `{"from": <phone>, "message_id": "M-1", "text": "my truck is 60 minutes late"}` | 200 | `HANDOFF`, reason `INCOMPLETE_FEASIBILITY_DATA`, a case reference. The pristine snapshot has no labour or consignee evidence, so the service refuses to guess. **This is the headline finding from the data audit, seen live.** |
| same request again | 200 | same reply, `"duplicate": true`, no second model call |
| same `message_id`, different text | 409 | `IDEMPOTENCY_MISMATCH` |
| wrong signature | 401 | `INVALID_SIGNATURE` (body never parsed) |
| 2001-character text | 422 | `INVALID_REQUEST` |
| unknown phone | 403 | `UNKNOWN_PHONE` |
| `POST /v1/driver/location` `{"from": <phone>, "lat": 27.4, "lon": 75.79, "recorded_at": "2026-08-04T09:34:00+05:30"}` | 200 | `{"status":"STORED","ping_id":1}` |
| same with `lat` 95 | 422 | `INVALID_REQUEST` |
| `GET /v1/coordinator/cases` without a token | 401 | `UNAUTHORIZED` |
| same with `Authorization: Bearer tok123` | 200 | the open case with its context: ETA 11:20, `missing: ["consignee_commitment","labour_availability"]`, recent turns |

To see the full hold-and-confirm flow on the real API you must first supply labour and consignee evidence (tables `labour_windows`, `consignee_commitments`; the demo console seeds labelled
`DEMO_FIXTURE` rows via `seed_demo_evidence` in `demo_ui.py`). The signing helper (Python):

```python
import hashlib, hmac, json, httpx
secret = b"demo-secret"
body = json.dumps({"from": "<driver phone>", "message_id": "M-1", "text": "my truck is 60 minutes late"}).encode()
sig = hmac.new(secret, body, hashlib.sha256).hexdigest()
print(httpx.post("http://127.0.0.1:8000/v1/driver/messages", content=body, headers={"x-signature": sig}).json())
```

Coordinators can also work cases from the command line: `python -m setuhaul.coordinator --db data/working.sqlite3 list|show|claim|resolve|reopen`.

## 7a. Sign-in, roles and the Telegram bot

Usage instructions for the screens are in the application itself: the **Wiki** link in the top menu (`/wiki`). Update
`src/setuhaul/auth/wiki_pages.py` when a screen changes.

Google sign-in has not been run against real Google. Telegram was run once against a real bot (2026-11-25, stand-in assistant); for a step-by-step
local setup see [telegram-local-setup.md](telegram-local-setup.md). The flows were also exercised with fakes
([verification/auth-roles-run.md](verification/auth-roles-run.md)).

### Settings

Read from the process environment by `create_app_from_env`. **The auth and Telegram variables below are not loaded from `.env` at start-up**: set them in the service's real environment (`.env.example` lists the names). The partner-mail settings are the exception: they read `.env` when first used.
Below, `<base>` is the public origin of your service, for example `https://haul.example.com`.

| Variable | Effect |
|---|---|
| `SETUHAUL_DB_PATH` | the migrated working database (also used by `set_admin_password.py`). Unset, or pointing at the sample `data/setuhaul.sqlite3`, the server and that script refuse to run |
| `SETUHAUL_REDIS_URL` | also holds the single-use sign-in state; if Redis is down the sign-in callback answers 503 `STATE_STORE_UNAVAILABLE` |
| `SETUHAUL_GOOGLE_CLIENT_ID`, `SETUHAUL_GOOGLE_CLIENT_SECRET` | Google sign-in; without both, `/auth/login` answers 503 `GOOGLE_NOT_CONFIGURED` |
| `SETUHAUL_SESSION_SECRET` | signs the sign-in state cookie; at least 32 bytes, otherwise Google sign-in stays off (a warning is logged) |
| `SETUHAUL_PUBLIC_URL` | origin only (`https://host[:port]`, no path or trailing slash); anything else, including a non-numeric port, stops start-up. Sets the redirect URI and the `Secure` cookie flag and is what the same-origin check compares scheme and host with. **Set it behind any proxy or HTTPS terminator**: without it the redirect URI comes from the request, and a proxy that rewrites `Host` can cause 403 `CROSS_SITE_REQUEST` on every change |
| `SETUHAUL_BOOTSTRAP_ADMIN_EMAIL` | at every start-up this address is an active admin: created if missing, its role and status restored if disabled or demoted. It does not clear a locked break-glass password (see Owner step 3) |
| `SETUHAUL_LOCAL_ADMIN` | `1` shows the break-glass password form on `/` and enables `POST /auth/local-admin`; otherwise that route is 404 |
| `SETUHAUL_PASSWORD_LOGIN` | `1` turns on email and password sign-in (below). Also needs `SETUHAUL_PUBLIC_URL`, `SETUHAUL_SMTP_USER` and `SETUHAUL_SMTP_PASSWORD`; a missing one is named in a warning and the feature stays off |
| `SETUHAUL_TELEGRAM_BOT_TOKEN` | the bot's token; used to send replies |
| `SETUHAUL_TELEGRAM_WEBHOOK_SECRET` | checked against Telegram's `X-Telegram-Bot-Api-Secret-Token` header; at least 16 characters. If either Telegram value is missing or the secret is short, `/v1/telegram/webhook` answers 503 |
| `SETUHAUL_TELEGRAM_AUTO_REGISTER` | `1` registers `<SETUHAUL_PUBLIC_URL>/v1/telegram/webhook` with Telegram at every start-up, using the bot token and secret in force (saved values over `.env`). Off by default. **Turn it on for one running copy only**: a bot has one webhook, and the last registration wins |
| `SETUHAUL_COORDINATOR_TOKENS` | JSON map `{"<token>": "<name>"}`: bearer access to `/v1/coordinator/cases...` for services. Not limited to facilities; names may not start with `web:` |
| `SETUHAUL_WEBHOOK_SECRET` | HMAC key of the signed driver and GPS channel (not Telegram); when unset, that channel refuses every request (401) |

### Email and password sign-in

With `SETUHAUL_PASSWORD_LOGIN=1` and the mail settings, inviting a user emails them a link to set a password, `/` offers email
and password beside Google, and **Forgot password?** emails a reset link. **Send password link** on the Users tab covers
resending an invitation. The mailbox can also be set on **Admin > Configuration > Email** (with **Send test email to me**); saved
values override `.env` and apply without a restart. The switch and `SETUHAUL_PUBLIC_URL` stay in `.env`. With Google sign-in also set up, `/` keeps its **Sign in with Google** button and the emails carry a
Google sign-in link too.

* A link works once: 48 hours for an invitation, 1 hour for a reset; a newer link cancels older ones. Disabling a user cancels
  theirs.
* Passwords are 12 to 1024 characters, stored only as scrypt hashes. Five wrong attempts lock password sign-in for 15 minutes;
  Google sign-in is unaffected. Setting a password ends that user's sessions. A password session lasts 8 hours.
* Emails: one per account per 2 minutes and 5 per hour; 30 forgot-password emails per hour in total, counted in Redis.
* Every role may use it, admins included, so an admin account is as safe as that admin's mailbox.
* While it is on, this form replaces the break-glass administrator form on `/`.
* It does not change the partner-email mode and needs no IMAP settings. Not run against a real mail server.

### The Model tab (admin, report only)

`/admin` > **Model** shows the model in use, health, calls and failures, response times (typical = median, and 95th percentile),
tokens, estimated cost, the limits in force, the last 50 driver messages and calls, and a LangSmith panel. Nothing there changes a
setting. Every model call and the message it belongs to are recorded as numbers and codes only (no message text, prompt, driver id or
key), and are kept until the database is deleted.

* **Existing databases need migration 014** (including the Telegram test database): run the upgrade command under **Database** below
  with its path. Until then messages work and the tab says to upgrade.
* **Cost** is an estimate from `SETUHAUL_MODEL_PRICE_IN_PER_MTOK` and `SETUHAUL_MODEL_PRICE_OUT_PER_MTOK`; without them it reads
  `price not set`. It is not the provider's bill.
* **LangSmith** is read only when you press **Load from LangSmith**, and only if tracing is already switched on
  (`SETUHAUL_LANGSMITH_ENABLED=true`, `SETUHAUL_LANGSMITH=approved`, `LANGSMITH_API_KEY`). It shows runs, failures and run times for
  24 hours. It has no reliable token or cost figures, and a handled model failure can appear as a successful run.
* The demo rebuilds its database at each start, so its history resets; its clock is frozen. The `--auto-login` demo user is not an
  admin, so sign in as an admin to see the tab.

### Configuration tab

With `SETUHAUL_SETTINGS_KEY` set (see `.env.example`), **Admin > Configuration** changes the model, LangSmith and Telegram settings
from the next message. A saved value overrides the environment until **Reset to .env**. The demo console shows the tab read only.

### Database

Migrations 011 to 014 add the tables (later ones, up to 020 for password links, are applied by the same command). `make_working_db` (section 7) creates a new migrated copy. To upgrade a working database you want to keep:

```powershell
python -c "from pathlib import Path; from setuhaul.db import open_connection, apply_migrations; c = open_connection(path=Path('data/working.sqlite3')); print(apply_migrations(c)); c.close()"
```

Migration 012 stops, changing nothing, if two projects have contract references differing only in case; rename one and run it again.

### Owner step 1: Google Cloud OAuth client

1. In the Google Cloud console, create or pick a project.
2. Configure the OAuth consent screen (scopes `openid`, `email`, `profile`). While the app is in testing, add each person as a test user.
3. Credentials > Create credentials > OAuth client ID > application type **Web application**.
4. Authorised redirect URI: `<base>/auth/callback`, exactly (for local trials, `http://localhost:8000/auth/callback`).
5. Put the client id and secret in `SETUHAUL_GOOGLE_CLIENT_ID` and `SETUHAUL_GOOGLE_CLIENT_SECRET`; generate `SETUHAUL_SESSION_SECRET` with
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

### Owner step 2: Telegram bot

1. In Telegram, message `@BotFather`, send `/newbot`, choose a name and username, and keep the token it gives you.
2. Generate a webhook secret: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
3. Set `SETUHAUL_TELEGRAM_BOT_TOKEN` and `SETUHAUL_TELEGRAM_WEBHOOK_SECRET` and start the service at a public HTTPS `<base>`.
4. Register the webhook (once, and again whenever the URL or secret changes). **Register webhook** on the Configuration tab does this
   using `SETUHAUL_PUBLIC_URL`, and `SETUHAUL_TELEGRAM_AUTO_REGISTER=1` does it at every start-up. A failed start-up registration does
   not stop the service: it is logged as a warning and the tab shows that the webhook needs registering. By hand instead, replace the
   placeholders (do not save the real values in a file or script):

```powershell
Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" -Body @{ url = "<base>/v1/telegram/webhook"; secret_token = "<WEBHOOK_SECRET>" }
```
```bash
curl -X POST "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" -d "url=<base>/v1/telegram/webhook" -d "secret_token=<WEBHOOK_SECRET>"
```

`getWebhookInfo` (same URL pattern) shows whether Telegram can reach it.

### Owner step 3: first admin and break-glass

1. Set `SETUHAUL_BOOTSTRAP_ADMIN_EMAIL` to your Google address and start the service.
2. Optional break-glass: run `python scripts/set_admin_password.py <your-email>` on the server, with `SETUHAUL_DB_PATH` set to the same
   database (the address must already be an active admin; the password is prompted for, 12 to 1024 characters), then restart with
   `SETUHAUL_LOCAL_ADMIN=1`. Five wrong attempts lock that account's password for 15 minutes; a locked and a wrong password look the same
   to the caller (the audit log tells them apart). The lock clears after the 15 minutes, or at once when `set_admin_password.py` sets a new
   password; restarting with `SETUHAUL_BOOTSTRAP_ADMIN_EMAIL` does not clear it. Every attempt on the enabled route is audited, including a
   malformed body or an over-long password (`LOCAL_ADMIN_REFUSED`, reason `BAD_REQUEST`, none of the submitted values kept).
   A break-glass session lasts 1 hour, a Google session 8 hours.

### Using it

1. Admin opens `<base>/`, signs in with Google, lands on `/admin` and invites users. A warehouse authority needs at least one facility.
2. The warehouse authority signs in, lands on `/warehouse` and adds a driver (name, phone, licence number and expiry, carrier, an existing vehicle). The driver is `PENDING_APPROVAL`.
3. The admin approves under **Pending drivers**.
4. The warehouse authority creates a project for one of their facilities and allocates the driver.
5. The driver opens the bot, sends any message, taps **Share my phone number**, and is linked if the number is theirs and matches exactly one eligible driver. Then they chat as usual.
6. To cut access: **Disable** a user (their sessions end at once); **Suspend** an approved onboarded driver (admin screen, **Onboarded drivers**;
   **Re-approve** undoes it while the licence is valid); or **Revoke Telegram** for a driver. A revoke is sticky: sharing the number again,
   from that or any other account, is refused until someone allowed to clicks **Allow re-link** (audited), after which the driver can link again.
7. To work a hand-off case: `/warehouse` > **Operations** > **Cases** > **Claim**, then choose an outcome, write the message the driver will
   receive and **Resolve**. Only the claimant can resolve. **Recently resolved** offers **Reopen** inside the reopen window (24 hours by default).
   A case that names no load is visible to admins only.
8. Cases that name no load belong to admins: the admin screen counts them under **Cases with no load**; work them on `/warehouse` > **Operations** > **Cases**.
9. Admins can **Re-enable** a disabled user and **Change facilities** for warehouse and security users; both are audited. A re-enabled
   user signs in again.

Rules enforced by the server:

* An address that was not invited gets `NOT_INVITED` and no user row. Unverified Google addresses are refused.
* A warehouse authority sees only their facilities; a case, board or project elsewhere answers "not found". Only a project's creator or an admin changes its drivers.
* Their driver chats are the messages recorded for loads going to their facilities, whoever added the driver. Each message is
  tagged when written with the load it is about (the one selected in the conversation, or the driver's only unfinished load)
  and that load's destination then. Messages with no tag, including older live chats, are seen by admins only.
* Their driver list shows drivers they added plus those project drivers, with the phone masked.
* **Revoke Telegram** and **Allow re-link** are for an admin or the warehouse authority who added the driver; supplied drivers: admin only.
  Allocating a driver to your project does not grant them. Anything else answers 404 `DRIVER_NOT_FOUND`.
* A suspended (rejected) or licence-expired onboarded driver is refused by the bot, by allocation and by the signed phone channel alike.
* Admins see everything and can also create projects and allocate.
* The drivers supplied with the data package (DRV001 to DRV015) have no onboarding profile: they count as approved while `ACTIVE` or `OFF_DUTY`, and their licence expiry is never checked.
* Every bot refusal is the same neutral message; the reason is only in the audit log.
* A change sent with the session cookie from another origin is refused (403 `CROSS_SITE_REQUEST`).
* Cases claimed from the web are stored as `web:<email>` and shown by display name.

### Known limitations

* **Driver notices are sent only when the bot is configured**, by one server process, every 5 seconds; running several server copies could send duplicates. Delivery is at least once: a crash between sending and recording can repeat a message. A failed send is tried again after 30 seconds, then 1, 3 and 5 minutes (five attempts, about ten minutes); the driver's later notices wait behind it. The waits are held in memory, so a restart retries at once, and with a frozen demo clock (`SETUHAUL_FAKE_NOW`) they never elapse. Notices more than 30 minutes old are marked `TOO_OLD` and not sent. Queued for a driver: *hold expired*, facility-change answers and closer-truck offers; booking, cancellation and escalation changes queue none (other operations, partner-warehouse and position messages are written into the chat history only). The demo does not send to Telegram.
* The generic `jobs/deliver_notifications` runner does not filter by recipient: a scheduler using it would also mark driver notices as sent without Telegram.
* Drivers have no web account or screen.
* An admin can create projects and allocate drivers, although one line of the design spec says admins only read projects.
* Drivers on projects created by an admin do not appear in any warehouse authority's chats.
* A driver is not tied to a warehouse: any eligible driver id can be allocated through the API by a warehouse authority to their own project. From then on, until release, that driver's new messages (including ones about other facilities' loads) are visible to them; earlier history is not.
* The screens cannot edit a driver, renew a licence, close a project, change a user's facilities or re-enable a disabled user (only the bootstrap admin comes back).
* A message sent to the bot by a revoked account gets the neutral refusal, with no share button and no audit row (only contact shares are audited).
* Migration 012 stops on contract references that differ only in case.
* Phone rules are India-first: a bare 10-digit number is read as `+91`, and a trunk `0` after any other country code is rejected (including numbers where that `0` is genuine, such as Italian landlines).
* The Google ID token signature is not verified. The token comes directly from Google's token endpoint over TLS (OIDC Core 3.1.3.7); issuer, audience, expiry, nonce and verified email are checked. A flow that receives tokens through the browser would need signature verification.
* Refused contact shares and messages from unlinked accounts are not rate limited; each refused share writes an audit row.
* Not run against real Google or a production HTTPS proxy. Telegram was tried once through a temporary tunnel, without a real AI model.

### Warehouse exceptions (signed-in warehouse screen, not the demo console)

On `/warehouse`, for your own facilities (an admin: all):

* **Layout:** the Dock board tab shows a compact board with an **Operations** panel beside it (Cases, Chats, Requests).
* **Block a dock** (Dock board tab, under the board): choose dock, type, from, optional to, and a reason, then **Check bookings**. The warning lists
  each affected booking and where it would move. **Block and move** applies exactly that list; if things changed meanwhile you are
  shown the new list. A booking that fits nowhere is freed and its shipment waits for a slot. Trucks already inside are left alone.
* **Reopen** ends one block early. Bookings already moved stay where they are.
* **Free a slot:** click the booked cell on the dock board, then **Free this slot**. The shipment stays and needs a new slot.
* **Delete a driver** (Drivers tab): lists the unfinished shipments that will be cancelled and asks for the driver's name. Admins
  may delete anyone; warehouse staff only drivers they added. Nothing is erased.
* **Restore** (admin): Admin screen, **Deleted drivers**. Cancelled shipments and project allocations do not come back, and
  Telegram must be allowed to re-link.

If a driver cannot be reached on Telegram after a slot is freed or moved, the screen says so: phone them.

## 8. Live model (*not run*)

To use a real model, copy `.env.example` to `.env` (git-ignored) and set `SETUHAUL_MODEL_PROVIDER` (`openai` or `anthropic`), `SETUHAUL_MODEL_ID`, `SETUHAUL_MODEL_API_KEY`; then restart the demo and switch to **Live model**.
`python scripts/run_live_model_scenario.py` runs one scripted scenario against the real model and refuses to run without these. Use synthetic data only; data protection for real driver messages is still an open question (Q9).
Recorded live runs (2026-11-20) are in `tests/traces/live-*.json` and replay clean; they are English-only evidence for one model, not proof of quality at scale.

## 9. Suggested 15-minute demonstration

| Minutes | Do | Say |
|---|---|---|
| 0-2 | Slide or talk: the problem, the rule "model understands, code decides" | the five rules in [knowledge-transfer.md](knowledge-transfer.md) section 4 |
| 2-6 | Scenario A on the console | no id was asked; one slot held from the database; "held" and "confirmed" were worded by the system |
| 6-8 | Scenario B (+3 min and sweep, then Confirm) | nothing promised that the database did not back; original stands |
| 8-9 | Scenario C | the database decides who wins |
| 9-11 | Scenario D, claim and resolve | a person takes over with context; the driver is told |
| 11-12 | GPS cross-check (Send ping 120 km, "20 minutes late") | GPS raises a flag and never overrides the driver |
| 12-14 | `show_phase8.py` and `replay_traces.py` | injection refused, outage safe, unbacked claim replaced; 123 evaluator checks |
| 14-15 | Real API: duplicate and `INCOMPLETE_FEASIBILITY_DATA` | what is **not** yet true: no labour feed, no real-model evidence at scale |

Always close with the limits: [client-readout.md](client-readout.md) lists what is proven and what is not.

## 10. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `/demo` shows the login page | sign in at `/`; if it says no sign-in is configured, set the `.env` values above or start with `--auto-login` |
| `/auth/login` answers 503 `GOOGLE_NOT_CONFIGURED` | client id or secret missing, or `SETUHAUL_SESSION_SECRET` shorter than 32 bytes |
| 403 `CROSS_SITE_REQUEST` from your own pages | set `SETUHAUL_PUBLIC_URL` to exactly the origin in the browser's address bar |
| Telegram webhook answers 503 / 401 | 503: token or secret missing, or secret shorter than 16 characters. 401: `setWebhook` used a different secret |
| Admin account disabled or demoted | restart with `SETUHAUL_BOOTSTRAP_ADMIN_EMAIL` set to it (restores role and status only) |
| Break-glass password locked | wait 15 minutes, or run `python scripts/set_admin_password.py <email>` on the server |
| `UNKNOWN_PHONE` for a valid driver on your own working database | identities not provisioned; call `provision_identities` (section 7) |
| `UnicodeEncodeError: 'charmap' codec` | set `PYTHONUTF8=1` before scripts that print Hindi |
| `[Errno 10048] ... only one usage of each socket address` | the port is taken by an earlier run; stop that process or pass another `--port` |
| Demo says nothing useful to "breakdown, repair 45 minutes" | the stand-in does not parse repair wording; use the Live model or the direct `derive_eta` call |
| Hold seems to last forever or lapse early | the clock is simulated; each message adds 20 s; use **+1 min** / **+3 min and sweep** |
| The board has no bookings and "original appointment ended" never appears | you are on the empty board (`--fresh`, or **Empty dock board** in the reset dialog); choose **Supplied schedule** to get the existing bookings back |
| Rows missing on the board | switch facility tab (Gurugram or Jaipur); demo drivers' loads are mostly Jaipur |
| `docs/verification/scenario-run.md` shows as modified | `run_scenario.py` rewrote it; `git checkout docs/verification/scenario-run.md` |
| Sandbox or CI differs from your machine | CI runs Windows and Ubuntu (`.github/workflows/ci.yml`); Ubuntu behaviour of later phases was not re-run here |

## 11. Auto-hold and changing facility

**Slots.** A delay report holds one slot (facility, dock, time, expiry in the reply). `Yes` books it; `No` releases it and holds the next,
searching again when the first three are used up; when nothing is left the case goes to operations and the original booking stands.

**Closer truck.** When a GPS ping shows an earlier slot is now reachable, one is held for 15 minutes and offered the same way. Nothing
changes unless the driver replies `Yes`; while a slot is already held, no further offer is made.

**Facility change.** The driver writes, for example, `Please change facility to Gurugram`. It is only a request (expires after 120 minutes).
A warehouse user of the **destination** facility, or an admin, approves or rejects it on `/warehouse` > Messages > Facility change requests.
On approval the load's destination changes and a slot is held there for 15 minutes; for the partner warehouse the usual email request is sent
instead. If nothing can be offered, the load goes back to its original facility with its original booking and a case is opened.

**If no booking comes of it** (the offer lapses, every slot is declined, or the partner declines or does not answer), the load goes back
to its original facility and the driver is told. This happens at the driver's next message or the next sweep job.

**Telegram.** With the bot configured, the server sends messages written outside a driver's turn (approval, rejection, closer-truck
offer, lapsed hold) within a few seconds, and runs the hold sweep itself (section 7a).

**Limits.** The partner request proposes the first full hour after the arrival.
