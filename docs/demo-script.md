# SetuHaul demo script

One script for a live demonstration: start the demo, connect Telegram, then show the examples. Windows PowerShell, from the
repository folder. Plan about 15 minutes for parts 2 and 3, and do part 4 (Telegram) setup **before** the audience arrives.

| To show | Go to | Time |
|---|---|---|
| A late driver being rescheduled, in the browser | parts 2 and 3 | 15 min |
| The same on a real phone | parts 4 and 5 | 5 min |
| The whole job: driver, project, dock slots, shipment, delay, gate | part 4, then part 6 | 15 min |

There are two separate servers. They have separate data and can run at the same time.

| | Web demo | Telegram demo |
|---|---|---|
| Address | `http://127.0.0.1:8800` | `http://localhost:8000` plus a public tunnel |
| Driver types in | a browser page | a real Telegram chat |
| Assistant | the model configured in `.env` | the scripted stand-in (set phrases only) |
| Sign-in | Google, backup admin password, or `--auto-login` | admin password |

**About the replies below.** They were produced with the scripted stand-in assistant and checked again on 2026-11-28. Held, confirmed, released and
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
| Warehouse | `http://127.0.0.1:8800/warehouse` | dock board with **Operations** (cases, chats, requests); Projects, Shipments and Drivers tabs |

Other drivers: add `?driver=DRV012` (Arjun Das) or `?driver=DRV014` (Pradeep Jat). The driver pages share one cookie, so to show two
drivers side by side use a private window for the second.

**Between examples press Reset demo** on the console. The demo clock starts at 4 August 2026, 09:35 IST.

The web demo keeps the supplied opening hours and cannot send Telegram messages, so a shipment created here never gets its
Dock Access Pass. Show the full setup with part 6.

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
options if you would like.* The stand-in then adds *How late are you, and which shipment is it?*
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

Do not choose **Assigned a slot** on the console: it records the outcome but books nothing. Part 6 shows it booking, on the
warehouse screen.

### Example 6: change of warehouse needs approval (3 min)

1. As Manoj: `Please change facility to Gurugram`. Reply: the request was sent, and the destination has **not** changed.
2. On the **Warehouse** tab, in the **Operations** panel, open **Requests** and press **Approve**.
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

This copies the sample data and gives your phone number to the sample driver Manoj Sharma. (Part 6 uses its own database, without that step.)

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
if os.environ.get("MY_PHONE"):
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
2. On the computer open `http://localhost:8000/`, sign in under **Administrator password**, then **Warehouse**, **Operations** panel, **Requests**.
3. Press **Approve**. Your phone gets the approval and a held slot within a few seconds, without you typing anything.
4. Send `Yes`.

**Say:** "The driver stays on Telegram. The warehouse decides on its own screen."

### T3 onwards: other phrases

Start from a fresh database (section 7) for each, so an earlier booking does not change the result. These phrases were checked on the
web demo with the same stand-in, not through Telegram.

| You send | Expected |
|---|---|
| `I will be about two hours late` | a later slot is held |
| `my truck is 60 minutes late`, then `never mind, keep my original slot` | the hold is released; the original appointment is unchanged |
| `please cancel my appointment` | handed to operations with a case reference |
| `bhai meri gaadi 60 minute late hai` | held-slot message in Hinglish |

## 6. From an empty project to the truck leaving

The warehouse sets everything up on screen, then the driver is delayed and passes the gate. It runs on the Telegram server of
part 4, with **its own database**, signed in as the admin. In this database your phone belongs to a **new** driver you add on
screen, so do not use the database of parts 4 and 5.

The steps below were run through the server on 2026-11-28 with a simulated Telegram, not with a real phone or by clicking in a
browser. Rehearse once.

### 6.1 Start

Finish 4.2 (tunnel) and 4.3 (settings). Then, in window 1, with no server running:

```powershell
$env:SETUHAUL_DB_PATH = "$HOME\setuhaul-walkthrough.sqlite3"
```

```powershell
Remove-Item Env:MY_PHONE -ErrorAction SilentlyContinue
```

Run the database script and the password command of 4.4. Then set the webhook and start the server (4.5). Open
`http://localhost:8000/` and sign in under **Administrator password**. That sign-in lasts one hour.

### 6.2 Add and approve a driver

1. **Warehouse > Drivers > Add a driver:**

| Field | Value |
|---|---|
| Name | `Demo Driver` |
| Phone | your Telegram number with country code (do not show it on screen) |
| Licence number | `DEMO-LIC-9001` |
| Licence expiry | 31 December 2027 |
| Carrier / Vehicle | `CAR001` / `VEH001` (registration `RJ14GT4101`) |

2. **Admin > Pending drivers:** **Approve**.

**Say:** "A new driver cannot be given work or use the bot until an administrator approves them."

### 6.3 Create a project and assign the driver

1. **Warehouse > Projects > New project:** Customer `Demo Foods`, Contract reference `DEMO-2026-01`, Facility
   `FAC-JAI-01`, Starts on 4 August 2026, Ends on 31 December 2026. Press **Create**.
2. In the project's row choose **Demo Driver** and press **Allocate**.

**Say:** "A project ties a customer contract to one warehouse and says which drivers work on it." Allocation controls which
drivers and chats a warehouse user sees; it is not required to create a shipment. **Release** removes a driver.

### 6.4 Link the driver's phone

In Telegram open your bot, send `hello`, then tap **Share my phone number**. Reply: *Thanks, this chat is now linked to your
driver profile. How can I help?*

### 6.5 Create dock slots

1. **Warehouse > Shipments > 1. Open availability:** Facility `FAC-JAI-01`, First and Last date **5 August 2026**, Docks
   `DOCK-JAI-D1` and `DOCK-JAI-D2`, From `08:00`, To `12:00`, Slot minutes `60`, Crews `4`, Equipment `Yes`. Press
   **Open availability**: 8 slots are created.
2. **Warehouse > Dock board:** under **Day** choose 5 August and press **Show**.
3. Under the board open **Round-the-clock slots**, type `4` in **Crews** and press **Save**. One-hour slots for every dock,
   day and night, are now created 14 days ahead and topped up automatically.

Do step 1 before step 3: afterwards those hours already have slots. Use 5 August in step 1, because 4 August already has
supplied slots and labour.

**Say:** "Slots can be opened by hand for chosen docks and hours, or created ahead from one number: how many docks can unload
at once. Without crews recorded, nothing is offered."

### 6.6 Create a shipment and confirm it

1. **Warehouse > Shipments > 2. New shipment:**

| Field | Value |
|---|---|
| Order number | `ORD900001` |
| Driver / Vehicle | `Demo Driver` / `VEH001` |
| Origin name / city | `Demo Mill` / `Ajmer` |
| Destination facility | `FAC-JAI-01` |
| Customer / Product | `Demo Foods` / `Packaged food` |
| Load (kg) / Dock type | `8000` / `STANDARD` |
| Unload minutes | `50` |
| Planned departure | 4 August 2026, 07:00 |
| Planned dock slot | D2, 04 Aug 2026 10:00 |

2. Press **Create shipment**. Your phone receives *Your Dock Access Pass for order ORD900001* and a code.
3. Copy the code and send it back to the bot. Reply: *Pass accepted. Order ORD900001: deliver to SetuHaul Jaipur Distribution
   Centre, planned 04 Aug 2026 10:00. How can I help?*
4. Reload **Shipments**: the state is **Confirmed**.

**Say:** "The order number identifies the load; the pass authorises the visit. The model never sees the code."

### 6.7 The driver is delayed

| You send | Reply |
|---|---|
| `my truck is 60 minutes late` | *I am holding 12:00-13:00 at dock D2 on 04 Aug 2026 ... for you until 09:38. It is not booked yet. ...* |
| `yes` | *Your appointment is confirmed: dock D2, 12:00-13:00 on 04 Aug 2026. Reference APT-H-...* |

The dock board shows the new booking, and the same pass now covers the new time.

### 6.8 What the warehouse can do

* **Write to a driver:** **Operations > Chats**, choose Demo Driver, type and **Send**. It reaches your phone.
* **Block a dock:** under the board open **Block a dock**: Dock `D4`, Type `Maintenance`, From 4 August 10:00, To 11:00,
  Reason `Leveller repair`. Press **Check bookings**: Manoj Sharma's booking `ORD-260804-006` would move to D1 at 13:00. Press
  **Block and move**.
* **Free a slot:** click that booking (D1, 13:00), press **Free this slot** and confirm. The shipment stays and needs a new
  slot.

Manoj has no Telegram in this database, so after both actions the screen says the driver could not be reached. **Say:** "It
tells the warehouse to phone, rather than assume the driver knows."

Also on screen: **Reopen** a block, **Delete** a driver (lists what will be cancelled first), and the admin's **Audit log**.

### 6.9 A person takes over, then the gate

The clock is frozen, so move it forward. In window 1 press Ctrl+C, then:

```powershell
$env:SETUHAUL_FAKE_NOW = "2026-08-04T11:05:00+05:30"
```

```powershell
& $py -m uvicorn --factory setuhaul.api:create_app_from_env --port 8000
```

Leave the tunnel running and sign in again.

1. Send `please cancel my appointment`. It is now too close to the slot, so the reply gives a case reference and nothing
   changes.
2. **Operations > Cases:** **Claim**, Outcome **Assigned a slot**, Slot to book **D4, 12:00**, type a message and **Resolve**.
   The booking moves and you are told on the phone.
3. Open `http://localhost:8000/gate`. Under **Arriving truck** type the pass, vehicle number `RJ14GT4101` and licence number
   `DEMO-LIC-9001`. Press **Check pass**, then **Check in**.
4. **Warehouse > Shipments:** **Unloading complete**.
5. **Gate > Trucks inside:** **Check out**.

To show a refusal first, press **Check pass** before moving the clock: *The truck is too early for its slot.*

**Say:** "One booking, followed from the warehouse setting it up to the truck leaving, and every step is audited."

## 7. Stop, restart, reset

- **Stop:** Ctrl+C in each window. Close window 1 to clear the token from memory.
- **Restart:** start the tunnel again (4.2), enter the settings (4.3, phone number not needed), then 4.5. Your chat stays linked.
- **Fresh Telegram database:** stop the server, run the command below, then 4.4, 4.5 (server only) and 4.6 again.
- **Fresh walkthrough (part 6):** stop the server, delete `$HOME\setuhaul-walkthrough.sqlite3*`, set `SETUHAUL_FAKE_NOW` back to
  `2026-08-04T09:35:00+05:30`, then start again at 6.1.

```powershell
Remove-Item "$HOME\setuhaul-telegram.sqlite3*"
```

## 8. If something goes wrong

| What you see | Fix |
|---|---|
| `No module named 'fastapi'` | you used another Python; run with `.\.venv\Scripts\python` |
| Web chat is switched off | the model settings are missing from `.env`; restart after adding them |
| `redirect_uri_mismatch` at Google | register `http://127.0.0.1:8800/auth/callback` and open the page as `127.0.0.1` |
| The bot never answers | check both windows are running; set the webhook again with the tunnel's current address |
| Server log shows 401 or 503 for the webhook | 401: run `setWebhook` again; 503: token or secret not set in this window |
| `The expression after '&' ... was not valid` | this window has no settings; run 4.3 |
| `[Errno 10048]` | the port is taken by an earlier server; stop it |
| Adding the driver says the phone number is already used | you are on the database of parts 4 and 5; start 6.1 again |
| **Pass not delivered** on the Shipments tab | the driver is not linked or the bot is off; do 6.4, then **Send new pass** |
| The gate says the truck is too early | the pass works from 60 minutes before the slot; move the clock (6.9) |

More: [telegram-local-setup.md](telegram-local-setup.md) and [running-and-demonstrating.md](running-and-demonstrating.md#10-troubleshooting).

## 9. Close the demo honestly

> "This runs on sample data with a simulated clock. Nothing is really emailed or texted from the web demo, the Telegram trial used a
> scripted assistant rather than an AI model, and this is a local trial, not a deployment."

Likely questions are answered in the [FAQ](faq.md).
