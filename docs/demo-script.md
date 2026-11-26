# SetuHaul demo script

One script for a live demonstration: start the demo, connect Telegram, then show the examples. Windows PowerShell, from the
repository folder. Plan about 15 minutes for parts 2 and 3, and do part 4 (Telegram) setup **before** the audience arrives.

There are two separate servers. They have separate data and can run at the same time.

| | Web demo | Telegram demo |
|---|---|---|
| Address | `http://127.0.0.1:8800` | `http://localhost:8000` plus a public tunnel |
| Driver types in | a browser page | a real Telegram chat |
| Assistant | the model configured in `.env` | the scripted stand-in (set phrases only) |
| Sign-in | Google, or `--auto-login` | admin password |

**About the replies below.** They were produced on 2026-11-26 with the scripted stand-in assistant. Held, confirmed, released and
hand-off messages are written by the system. If your `.env` has a model, the web demo uses it: its questions may be worded
differently and it may understand more or less than shown here, so rehearse once. Reference numbers differ on every run, and times and
docks depend on the board and clock.

Use made-up text only. With a model configured, what you type is sent to that provider.

## 1. Before the demo

Install once (skip if `.venv` already exists):

```powershell
py -3.12 -m venv .venv
```

```powershell
.\.venv\Scripts\python -m pip install -e ".[dev]"
```

Checklist:

- [ ] `.env` has `SETUHAUL_MODEL_PROVIDER`, `SETUHAUL_MODEL_ID` and `SETUHAUL_MODEL_API_KEY` (without them the web chat is off).
- [ ] For Google sign-in, `.env` also has the client id, client secret and `SETUHAUL_BOOTSTRAP_ADMIN_EMAIL`.
- [ ] Do not show `.env`, the bot token or BotFather on screen.
- [ ] Telegram setup (part 4) done and tested.

## 2. Start the web demo

With real Google sign-in (shows the login screen):

```powershell
.\.venv\Scripts\python scripts\run_demo.py
```

Or without sign-in, for rehearsal on your own computer only:

```powershell
.\.venv\Scripts\python scripts\run_demo.py --auto-login
```

The terminal shows the assistant model in use. Open these tabs:

| Tab | Address | Shows |
|---|---|---|
| Console | `http://127.0.0.1:8800/demo` (with `--auto-login`, first open `/demo/login`) | read-only driver chat, dock board, operations |
| Driver Manoj | `http://127.0.0.1:8800/demo/driver-login` | where you type as the driver |
| Warehouse | `http://127.0.0.1:8800/warehouse` | requests to approve, chats, projects, drivers |

Other drivers: add `?driver=DRV012` (Arjun Das) or `?driver=DRV014` (Pradeep Jat). The driver pages share one cookie, so to show two
drivers side by side use a private window for the second.

**Between examples press Reset demo** on the console. The demo clock starts at 4 August 2026, 09:35 IST.

### Opening line

> "A driver is running late and the dock appointment no longer works. Today someone phones around. Here the driver sends one message.
> The model understands and drafts; code decides what is allowed and what is true."

## 3. Web examples

### Example 1: late, one slot held, declined, confirmed (3 min)

| Driver types | Reply |
|---|---|
| `my truck is 60 minutes late` | *I am holding 12:00-13:00 at dock D2 on 04 Aug 2026, SetuHaul Jaipur Distribution Centre, for you until 09:38. It is not booked yet. Reply Yes to confirm, or No for the next slot.* |
| `no` | the same message for **dock D4** |
| `yes, please confirm` | *Your appointment is confirmed: dock D4, 12:00-13:00 on 04 Aug 2026. Reference APT-H-... Warehouse: SetuHaul Jaipur Distribution Centre.* |

**Show:** the board cell turns Held, then Booked. Open **Operations > Activity**.
**Say:** "The driver was never asked who they are. Nothing was booked until they said yes."

Variations: `I will be about two hours late` (holds 13:00-14:00 at D1) and `running half an hour late` (12:00-13:00 at D2). Both add
*Note: your truck's GPS suggests your arrival differs from your report by about N minutes.* **Say:** "GPS raises a flag; it does not
overrule the driver."

### Example 2: an unclear answer books nothing (1 min)

| Driver types | Reply |
|---|---|
| `my truck is 60 minutes late` | one slot held |
| `not okay` | no booking; the stand-in asks *How late are you, and which shipment is it?* and the slot stays held |
| `yes` | the held slot is confirmed |

### Example 3: the hold lapses (1 min)

1. `my truck is 60 minutes late`
2. On the console press **+3 min and sweep**.
3. `yes, please confirm`

Reply: *Your hold on that slot has lapsed. No booking has changed, and your original appointment still stands. I can search for fresh
options if you would like.*
**Say:** "It never promises something the database does not back."

### Example 4: two drivers, one dock (2 min)

1. Manoj: `my truck is 60 minutes late`, and do not confirm. He holds **D2**.
2. Arjun (`?driver=DRV012`): the same message. He is offered **D4**, not D2.

**Say:** "The database, not the model, guarantees one holder per slot."

### Example 5: no slot fits, a person takes over (2 min)

1. As Pradeep (`?driver=DRV014`): `my truck is 60 minutes late`
2. Reply: *I cannot sort this out automatically, so I have passed it to the operations team with the details. Your case reference is
   ESC-... Nothing on your booking has been changed by this. You will get updates here in this chat.*
3. On the console open **Operations > Cases**, press **Claim**, then **Resolve** with a note. The driver's chat is updated.

### Example 6: change of warehouse needs approval (3 min)

1. As Manoj: `Please change facility to Gurugram`. Reply: the request was sent, and the destination has **not** changed.
2. On the **Warehouse** tab open **Messages** and press **Approve**.
3. The driver's chat shows *The move to SetuHaul Gurugram Cross-Dock is approved. I am holding 16:00-17:00 at dock D1 ...*
4. `yes` confirms it at Gurugram.

To show a refusal instead, press **Reject**: *The move to SetuHaul Gurugram Cross-Dock was not approved. Your load is still going to
SetuHaul Jaipur Distribution Centre; your booking is unchanged.*

### Example 7: keep or cancel (1 min)

| Driver types | Reply |
|---|---|
| `my truck is 60 minutes late`, then `never mind, keep my original slot` | *I have released the hold. Your original appointment is unchanged.* |
| `please cancel my appointment` (after a reset) | goes to a person with a case reference; nothing is changed |

### Example 8: Hinglish (1 min)

`bhai meri gaadi 60 minute late hai` gets the held-slot message in Hinglish: *Maine aapke liye ... slot 09:38 tak hold kiya hai.
Abhi book nahi hua hai. ...* No native speaker has reviewed the wording.

### Example 9: empty dock board (optional)

**Reset demo** > **Empty dock board** (or start with `--fresh`). `my truck is 60 minutes late` then holds 12:00-13:00 at **D1**.

### Example 10: safety proofs, in a terminal (optional, 2 min)

```powershell
$env:PYTHONUTF8 = "1"; .\.venv\Scripts\python scripts\show_phase8.py
```

Prompt injection refused, model outage handled safely, an unbacked "you are confirmed" replaced.

```powershell
.\.venv\Scripts\python scripts\show_phase5.py
```

The database rejects a second holder for the same slot.

```powershell
.\.venv\Scripts\python scripts\replay_traces.py
```

Recorded conversations replayed and scored.

## 4. Connect Telegram

Do this before the demo. You need a Telegram account and two PowerShell windows: window 1 for the server, window 2 for the tunnel.

### 4.1 Once only

1. In Telegram open **@BotFather**, send `/newbot`, choose a name and a username ending in `bot`, and keep the token private.
2. Install the tunnel:

```powershell
winget install --id Cloudflare.cloudflared
```

### 4.2 Window 2: start the tunnel

```powershell
& "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://localhost:8000
```

Copy the `https://….trycloudflare.com` address and leave the window open. The address changes every time, and anyone who has it can
reach your server while it runs.

### 4.3 Window 1: settings

```powershell
cd C:\GitHub\SetuHaul_Rescheduling_Service
$py = ".\.venv\Scripts\python.exe"
$env:PYTHONUTF8 = "1"
$env:SETUHAUL_DB_PATH = "$HOME\setuhaul-telegram.sqlite3"
$env:SETUHAUL_REDIS_URL = "fakeredis://"
$env:SETUHAUL_USE_STUB_MODEL = "1"
$env:SETUHAUL_FAKE_NOW = "2026-08-04T09:35:00+05:30"
$env:SETUHAUL_LOCAL_ADMIN = "1"
$env:SETUHAUL_WEBHOOK_SECRET = & $py -c "import secrets; print(secrets.token_urlsafe(32))"
$env:SETUHAUL_TELEGRAM_WEBHOOK_SECRET = & $py -c "import secrets; print(secrets.token_urlsafe(32))"
```

Run these one at a time and answer each prompt:

```powershell
$env:SETUHAUL_TELEGRAM_BOT_TOKEN = Read-Host "Bot token"
```

```powershell
$env:MY_PHONE = Read-Host "Telegram phone number with country code (+91...)"
```

```powershell
$env:ADMIN_EMAIL = Read-Host "Admin email"
```

```powershell
Clear-Host
```

### 4.4 Window 1: create the test database

This copies the sample data and gives your phone number to the sample driver Manoj Sharma.

```powershell
@'
import os
from pathlib import Path
from setuhaul.auth import users
from setuhaul.clock import FakeClock
from setuhaul.db import make_working_db, open_connection, parse_operational_timestamp
from setuhaul.demo_ui import seed_demo_evidence
from setuhaul.identity import provision_identities
p = Path(os.environ["SETUHAUL_DB_PATH"])
if p.exists():
    raise SystemExit("The test database already exists. Delete it first to start again.")
make_working_db(p, Path("data/setuhaul.sqlite3"))
c = open_connection(path=p)
seed_demo_evidence(c)
c.execute("update drivers set phone=? where driver_id='DRV006'", (os.environ["MY_PHONE"],))
c.commit()
provision_identities(c)
users.bootstrap_admin(c, os.environ["ADMIN_EMAIL"], FakeClock(parse_operational_timestamp(os.environ["SETUHAUL_FAKE_NOW"])))
c.commit()
c.close()
print("ready")
'@ | & $py -
```

Expected: `ready`. Then set the admin password (12 characters or more; nothing appears as you type):

```powershell
& $py scripts\set_admin_password.py $env:ADMIN_EMAIL
```

### 4.5 Window 1: connect and start

Put your tunnel address here, with no `/` at the end:

```powershell
$base = "https://YOUR-NAME.trycloudflare.com"
```

```powershell
Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$($env:SETUHAUL_TELEGRAM_BOT_TOKEN)/setWebhook" -Body @{ url = "$base/v1/telegram/webhook"; secret_token = $env:SETUHAUL_TELEGRAM_WEBHOOK_SECRET }
```

Expected: `Webhook was set`.

```powershell
& $py -m uvicorn --factory setuhaul.api:create_app_from_env --port 8000
```

### 4.6 Link your chat

Open `https://t.me/<your_bot_username>` (not BotFather) and tap **Start**.

| You | Bot |
|---|---|
| `hello` | asks you to share your phone number |
| tap **Share my phone number** | *Thanks, this chat is now linked to your driver profile. How can I help?* |

## 5. Telegram examples

The stand-in understands only set phrases like these. This setup's clock is frozen, so **holds never lapse** here; use web
example 3 for that. No GPS location or closer-truck offer comes from Telegram.

### T1: delay and booking

| You send | What happens |
|---|---|
| `I am running late by 10 min` | one slot is held (the first trial held 12:00-13:00 at D2), with a GPS note |
| `No` | the next slot is held instead |
| `Yes` | *Your appointment is confirmed: dock ..., Reference APT-H-... Warehouse: SetuHaul Jaipur Distribution Centre.* |

### T2: change warehouse, approved on the computer

1. Send `Please change facility to Gurugram`.
2. On the computer open `http://localhost:8000/`, sign in under **Administrator password**, then **Warehouse** > **Messages**.
3. Press **Approve**. Your phone gets the approval and a held slot within a few seconds, without you typing anything.
4. Send `Yes`.

**Say:** "The driver stays on Telegram. The warehouse decides on its own screen."

### T3 onwards: other phrases

Start from a fresh database (section 6) for each, so an earlier booking does not change the result. These phrases were checked on the
web demo with the same stand-in, not through Telegram.

| You send | Expected |
|---|---|
| `I will be about two hours late` | a later slot is held |
| `my truck is 60 minutes late`, then `never mind, keep my original slot` | the hold is released; the original appointment is unchanged |
| `please cancel my appointment` | handed to operations with a case reference |
| `bhai meri gaadi 60 minute late hai` | held-slot message in Hinglish |

## 6. Stop, restart, reset

- **Stop:** Ctrl+C in each window. Close window 1 to clear the token from memory.
- **Restart:** start the tunnel again (4.2), enter the settings (4.3, phone number not needed), then 4.5. Your chat stays linked.
- **Fresh Telegram database:** stop the server, run the command below, then 4.4, 4.5 (server only) and 4.6 again.

```powershell
Remove-Item "$HOME\setuhaul-telegram.sqlite3*"
```

## 7. If something goes wrong

| What you see | Fix |
|---|---|
| `No module named 'fastapi'` | you used another Python; run with `.\.venv\Scripts\python` |
| Web chat is switched off | the model settings are missing from `.env`; restart after adding them |
| `redirect_uri_mismatch` at Google | register `http://127.0.0.1:8800/auth/callback` and open the page as `127.0.0.1` |
| The bot never answers | check both windows are running; set the webhook again with the tunnel's current address |
| Server log shows 401 or 503 for the webhook | 401: run `setWebhook` again; 503: token or secret not set in this window |
| `The expression after '&' ... was not valid` | this window has no settings; run 4.3 |
| `[Errno 10048]` | the port is taken by an earlier server; stop it |

More: [telegram-local-setup.md](telegram-local-setup.md) and [running-and-demonstrating.md](running-and-demonstrating.md#10-troubleshooting).

## 8. Close the demo honestly

> "This runs on sample data with a simulated clock. Nothing is really emailed or texted from the web demo, the Telegram trial used a
> scripted assistant rather than an AI model, and this is a local trial, not a deployment."

Likely questions are answered in the [FAQ](faq.md).
