# Run SetuHaul locally and connect Telegram

Windows PowerShell, from the repository folder. Replies quoted below are from a trial run on 2026-11-25 with a real Telegram bot,
the rule-based stand-in assistant and a fixed demo clock. A real AI model has **not** been tried with Telegram.

## 0. What you need

* Python 3.12 and this repository.
* For Telegram only: a Telegram account with a phone number, and `cloudflared` (installed in step B2).

## A. Run locally without Telegram

**Install (once):**

```powershell
py -3.12 -m venv .venv
```

```powershell
.\.venv\Scripts\python -m pip install -e ".[dev]"
```

**Check the installation (optional):**

```powershell
.\.venv\Scripts\python -m pytest -q
```

**Start the demo:**

```powershell
.\.venv\Scripts\python scripts\run_demo.py --auto-login
```

| Page | Use |
|---|---|
| `http://127.0.0.1:8800/demo/login` | the demonstration console (signs you in as a local demo user) |
| `http://127.0.0.1:8800/demo/driver-login` | send messages as a demo driver (add `?driver=DRV007` for another) |

`--auto-login` has **no authentication**: use it only on your own computer. The demo reads `.env`, so it uses a real model if one is
configured there. It never sends to Telegram. Press Ctrl+C to stop. More scenarios: [running-and-demonstrating.md](running-and-demonstrating.md).

## B. Connect Telegram

You need **two PowerShell windows**: one for the server (keeps your settings) and one for the tunnel.

```
Telegram  -->  https://<name>.trycloudflare.com  -->  tunnel  -->  http://localhost:8000 (your server)
```

### B1. Create the bot

1. In Telegram, open **@BotFather**.
2. Send `/newbot`, then choose a name and a username ending in `bot`.
3. Keep the token it gives you. Do not put it in a file, a chat or a screenshot. If it is ever shown, send `/revoke` to BotFather.

### B2. Install the tunnel (once)

```powershell
winget install --id Cloudflare.cloudflared
```

### B3. Start the tunnel (window 2)

```powershell
& "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://localhost:8000
```

Copy the `https://….trycloudflare.com` address printed in the box under *Your quick Tunnel has been created!* Leave the window
running. **The address changes every time the tunnel starts.** While it runs, anyone with the address can reach your server.

### B4. Enter the settings (window 1)

Paste the whole block:

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

Then run these **one at a time**, answering each prompt:

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

These settings exist only in this window. **Run every later command here.** Without `SETUHAUL_DB_PATH` the server and the
password script refuse to run, to protect the sample database `data/setuhaul.sqlite3`.

### B5. Create the test database (once)

This copies the sample data and gives your phone number to the sample driver Manoj Sharma (`DRV006`).

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

Expected: `ready`.

### B6. Set the admin password (once per database)

At least 12 characters. Nothing appears while you type.

```powershell
& $py scripts\set_admin_password.py $env:ADMIN_EMAIL
```

Expected: `Password set for <your email>.`

### B7. Point Telegram at your server

Replace the address with yours from B3, with no `/` at the end:

```powershell
$base = "https://YOUR-NAME.trycloudflare.com"
```

```powershell
Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$($env:SETUHAUL_TELEGRAM_BOT_TOKEN)/setWebhook" -Body @{ url = "$base/v1/telegram/webhook"; secret_token = $env:SETUHAUL_TELEGRAM_WEBHOOK_SECRET }
```

Expected: `ok : True`, `Webhook was set`. If the address cannot be resolved, wait a minute and run it again.

### B8. Start the server

```powershell
& $py -m uvicorn --factory setuhaul.api:create_app_from_env --port 8000
```

Expected: `Uvicorn running on http://127.0.0.1:8000`. Each Telegram message then logs `POST /v1/telegram/webhook`.

## C. Messages in Telegram

### C1. Open your bot's own chat

Open `https://t.me/<your_bot_username>` (not the BotFather chat) and tap **Start**. Send messages **without** the `@botname` in front.

### C2. Link your chat to the driver

| You | The bot |
|---|---|
| `hello` | *To use SetuHaul, please share your phone number with the button below so I can link this chat to your driver profile.* |
| tap **Share my phone number** | *Thanks, this chat is now linked to your driver profile. How can I help?* |

The shared number must be the one entered in B4.

### C3. Report a delay and book a slot

**You:** `I am running late by 10 min` (or `I will be about two hours late`)

**Bot:** *I am holding 12:00-13:00 at dock D2 on 04 Aug 2026, SetuHaul Jaipur Distribution Centre, for you until 09:38. It is not
booked yet. Reply Yes to confirm, or No for the next slot.*
*Note: your truck's GPS suggests your arrival differs from your report by about 62 minutes.*

The note comes from the sample GPS data. Slot times and docks can differ from run to run and with the delay you report.

| You | What happens |
|---|---|
| `No` | that slot is released and the next one is held and offered the same way |
| `Yes` | *Your appointment is confirmed: dock D2, 12:00-13:00 on 04 Aug 2026. Reference APT-H-…. Warehouse: SetuHaul Jaipur Distribution Centre.* |

Nothing is booked until you reply `Yes`.

### C4. Ask to change warehouse

**You:** `Please change facility to Gurugram`

**Bot:** *I have asked SetuHaul Gurugram Cross-Dock to take this load. Your destination has not changed yet: it is still SetuHaul
Jaipur Distribution Centre until they approve.*

### C5. Approve it (on the computer, not in Telegram)

1. Open `http://localhost:8000/`.
2. Under **Administrator password**, enter the admin email and the password from B6. This sign-in lasts one hour.
3. Click **Warehouse** (or open `http://localhost:8000/warehouse`), then **Messages**.
4. Find the facility change request and press **Approve** (or **Reject**).

Within a few seconds Telegram receives a message without you writing anything:

* Approved: *The move to SetuHaul Gurugram Cross-Dock is approved. I am holding 16:00-17:00 at dock D1 on 04 Aug 2026 until 09:50.
  It is not booked yet. Reply Yes to confirm, or No for the next slot.*
* Rejected (wording from the code, not seen in the trial): *The move to SetuHaul Gurugram Cross-Dock was not approved. Your load is
  still going to SetuHaul Jaipur Distribution Centre; your booking is unchanged.*

### C6. Confirm the new slot

**You:** `Yes`

**Bot:** *Your appointment is confirmed: dock D1, 16:00-17:00 on 04 Aug 2026. Reference APT-H-…. Warehouse: SetuHaul Gurugram
Cross-Dock.*

## D. Stop, restart, remove

**Stop:** press Ctrl+C in both windows. Close window 1 to clear the token and secrets from memory.

**Restart later** (the test database is kept):

1. B3: start the tunnel and copy the **new** address.
2. B4: enter the settings again (the phone number is not needed).
3. B7: set the webhook again (the address and secret are new).
4. B8: start the server. Your chat is still linked; skip B5 and B6.

**Remove the trial** (server stopped first):

```powershell
Remove-Item "$HOME\setuhaul-telegram.sqlite3*"
```

To also disconnect the bot from the old address, run this in a window that has the token (standard Bot API call, not tried in the trial):

```powershell
Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$($env:SETUHAUL_TELEGRAM_BOT_TOKEN)/deleteWebhook"
```

## E. If something goes wrong

| What you see | Cause and fix |
|---|---|
| `The expression after '&' in a pipeline element produced an object that was not valid` | `$py` is not set: this window has no settings. Run B4 here |
| `cloudflared : The term 'cloudflared' is not recognized` | the window is older than the installation. Use the full path (B3) |
| `FileNotFoundError` naming `setuhaul-telegram.sqlite3` while creating the database | the copy source was left out. Use the B5 block exactly |
| `Refused: the password must be at least 12 characters` | choose a longer password |
| BotFather answers your `hello` with a command list | you are in the BotFather chat. Open your bot's own chat (C1) |
| The bot never answers | check both windows are running, then set the webhook again with the tunnel's **current** address (B7) |
| Webhook answers 503 / 401 in the server log | 503: token or secret missing. 401: the server restarted with a new secret; run B7 again |
| `[Errno 10048]` when starting | port 8000 is taken; stop the earlier server |
| `404 Not Found` for addresses such as `/v1/telemetry/stats` | another program on your computer is polling port 8000; ignore |
| `SETUHAUL_DB_PATH is not set to a working copy` | this window has no settings. Run B4 here |

## F. Limits of this setup

* The stand-in assistant understands only set phrases like those above.
* The clock is fixed at 4 August 2026 to match the sample data, so holds never expire and a failed send is never retried.
* No closer-truck offer appears: Telegram sends no location here.
* This is a local trial, not a deployment: one server process, an in-memory Redis stand-in and a temporary public address.
* Settings and server behaviour in detail: [running-and-demonstrating.md](running-and-demonstrating.md#7a-sign-in-roles-and-the-telegram-bot).
