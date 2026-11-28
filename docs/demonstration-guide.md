# Demonstration guide (Production)

See also the [FAQ](faq.md) for the questions an audience is likely to ask.

How to demonstrate the live **SetuHaul** production service end-to-end across the **Warehouse** (`/warehouse`), **Admin** (`/admin`), **Gate** (`/gate`), and **Driver Telegram** channels, how to explain the architecture, and how to verify edge cases.

Use **test/synthetic orders and drivers** so real operations are not disrupted. Production runs on the real wall clock (IST), real authentication, and the configured LLM + Telegram bot.

## 0. Before you start

1. **Sign in to the production web app:**
   - Open `https://<your-production-host>/` and sign in as an **Admin** or **Warehouse Authority** (email/password or Google sign-in).
   - Open `/warehouse` (Dock board, Operations, Shipments, Drivers, Projects).
   - Optionally open `/gate` in another browser window/profile signed in as a **Security** user to demonstrate Dock Access Pass gate check-in.
2. **Confirm model & Telegram are configured (`/admin → Settings`):**
   - **Model:** active provider + API key (`SETUHAUL_MODEL_PROVIDER`, `SETUHAUL_MODEL_ID`, `SETUHAUL_MODEL_API_KEY`).
   - **Telegram:** bot token + webhook registered, and at least one approved, active, licensed test driver linked to Telegram (a second linked test driver is helpful for Scenario C).
3. **Prepare open dock slots** using Scenario **1. Open availability** (or **Dock board → Round-the-clock slots**).

## 1. Talk track (about 10 minutes)

1. **The problem.** A driver is delayed or rerouted and the dock appointment no longer works. Today someone phones around.
2. **The rule.** The model understands and drafts; deterministic code decides what is allowed and what is true.
3. **Setup (Steps 1 & 2).** Open dock availability and create a shipment; the driver receives a Dock Access Pass on Telegram and confirms it.
4. **Scenario A.** The driver is identified from Telegram; one slot is held; nothing is booked until `"yes"`.
5. **Scenario B.** After 3 minutes without confirmation, the hold lapses and the original appointment is untouched.
6. **Scenario C.** A second driver is offered a different dock: held capacity is already reserved at the database level.
7. **Scenario D.** No slot fits, so Operations takes over with full context and assigns a slot (or cancels and frees the dock).
8. **Scenario E.** Changing warehouse (via driver request or directly in Shipments) frees the old slot immediately and allows allocating a new slot.
9. **Scenario I.** Full Warehouse Authority controls: Dock Board `×` free slot, Operations cancellation, direct warehouse change, slot allocation, and confirmed shipment cancellation.

## 2. Scenarios

All scenarios below run on the live production system using `/warehouse` and the driver's Telegram chat. Exact times and dock codes depend on your current clock and available slots.

### 1. Open availability

Open `/warehouse` → **Shipments** → **1. Open availability**:

| Step | Action (warehouse screen) | You should see |
|---|---|---|
| 1 | Choose **Facility**, **First date**, and **Last date** (at most 14 days). | Only docks belonging to that facility are enabled. |
| 2 | Select one or more **Docks**, enter **From** and **To** times (e.g. `00:00` to `00:00` for a full 24-hour day), **Slot minutes** (`60`), **Crews** (e.g. `4`), and **Equipment available** (`Yes`). | Existing labour windows and overlapping slots are preserved; missing slots are added. |
| 3 | Click **Open availability**. | Status reports how many slots were added. On the **Dock board** (which always shows all 24 hourly columns `00:00`–`23:00`), the opened hours appear green (**Free**) and are available in the **Planned dock slot** picker. *(Alternatively, under **Dock board → Round-the-clock slots**, save a **Crews** count to auto-create 24/7 1-hour slots for the next 14 days.)* |

### 2. New shipment

Open `/warehouse` → **Shipments** → **2. New shipment**:

| Step | Action (warehouse screen) | You should see |
|---|---|---|
| 1 | Enter **Order number** (`ORD` followed by at least 6 digits, e.g. `ORD-261129-001`), choose an eligible **Driver** and matching **Vehicle**, and enter **Origin name**, **Origin city**, **Destination facility**, **Customer**, and **Product**. | Vehicle list automatically filters to the driver's carrier; slot list filters to the destination facility. |
| 2 | Enter **Load (kg)**, **Pallets** (optional), **Dock type** (`STANDARD`, `REEFER`, `HEAVY`, or `ANY`), **Refrigerated**, **Priority**, **Unload minutes**, **Planned departure** (must be before the slot start), and pick a **Planned dock slot**. | Feasibility rules verify dock type, refrigeration, weight, unload duration, and labour before committing. |
| 3 | Click **Create shipment**. | The shipment appears in the **Shipments** table (`Waiting for driver`), the slot turns **Booked** on the **Dock board**, and the driver receives a **Dock Access Pass** on Telegram. |
| 4 | As the driver on Telegram, reply with the Dock Access Pass code. | The bot confirms the shipment (`Pass accepted. Order ...`), and the **Shipments** table state updates to **Confirmed**. |

### A. Happy path: late, held, declined, confirmed

| Step | Type (driver Telegram chat) | You should see |
|---|---|---|
| 1 | `my truck is 60 minutes late` | *I am holding HH:MM-HH:MM at dock D... on DD MMM YYYY, \<Facility\>, for you until HH:MM. It is not booked yet. Reply Yes to confirm, or No for the next slot.* On `/warehouse` → **Dock board**, that cell turns **Held** (yellow striped) while the original slot stays **Booked**. |
| 2 | `no` | The first hold is released (cell turns **Free** again) and the next feasible dock/time is held. |
| 3 | `yes, please confirm` | *Your appointment is confirmed: dock D..., HH:MM-HH:MM on DD MMM YYYY. Reference APT-H-.... Warehouse: \<Facility\>.* The old slot turns **Free**, the new slot turns **Booked**, and the Dock Access Pass window moves automatically to the new slot. |

**What this proves:** identity from Telegram, arrival time derived from planned time + GPS floor, deterministic feasibility, transactional hold and atomic swap on confirmation, and immutable audit/event history.

### B. The hold lapses (3-minute TTL)

1. As the driver on Telegram, send `my truck is 60 minutes late`. A slot is held for **3 minutes (180 seconds)**.
2. Wait **3 minutes** without replying `yes`.
3. Send `yes, please confirm`.

You should see: *Your hold on that slot has lapsed. No booking has changed, and your original appointment still stands.* The background expiry sweep (or lazy expiry on the late `yes`) frees the held cell on the **Dock board** and leaves the original booking untouched.

### C. Held capacity is not offered to anyone else

1. As **Driver 1** on Telegram, send `my truck is 60 minutes late` and do **not** confirm yet. Dock A is now **Held**.
2. As **Driver 2** on Telegram (same warehouse, overlapping arrival time), send `my truck is 60 minutes late`.

You should see: Driver 2 is offered a **different dock/slot**, never the one held by Driver 1. The database's active-slot unique index guarantees one holder per slot.

### D. No slot fits: hand-off to Operations

1. Create or use a shipment whose dock type/weight/time has **no** feasible open slot (for example, a `HEAVY` load when the heavy dock is blocked or booked).
2. As the driver on Telegram, send `my truck is 60 minutes late`.
3. The driver receives a case reference (`ESC-...`) and is told nothing changed.
4. On `/warehouse` → **Operations → Cases**, click **Claim**, choose **Assigned a slot**, pick an open slot (up to 200 upcoming slots are listed), write a note to the driver, and click **Resolve**.

You should see: the **Dock board** updates immediately, the driver receives the confirmation in Telegram, and the case closes.

### E. Driver-requested warehouse change

1. As the driver on Telegram, send `Please change facility to <Destination Warehouse>`.
   - Reply: the request is sent and the destination has **not** changed yet.
2. On `/warehouse` → **Operations → Requests** (signed in as an Admin or a Warehouse Authority for the destination warehouse), click **Approve**.
   - The old dock slot at the origin warehouse is **freed immediately** and the old gate pass is revoked (`WAREHOUSE_CHANGED`).
   - If a slot is available at the new warehouse, it is held for **15 minutes** and the driver is messaged on Telegram (`Reply Yes to confirm, or No for the next slot`).
   - If no slot is available (or the hold lapses), the shipment **stays assigned to the new warehouse** (it does not revert) and Operations can allocate a slot when one opens.
3. As the driver, reply `yes` to confirm the held slot.
   - The booking is confirmed at the new warehouse and a **fresh Dock Access Pass** is immediately sent to the driver on Telegram.

### F. Keep or cancel (driver-initiated)

- **Release a pending hold:** Send `my truck is 60 minutes late`, then `never mind, keep my original slot`.
  - Reply: *I have released the hold. Your original appointment is unchanged.* The held cell turns **Free** on the Dock Board.
- **Cancel a confirmed booking:** Send `please cancel my appointment`.
  - Outside the cancellation cut-off (with no penalty/exceptional commitment), the appointment is cancelled immediately, the dock slot turns **Free**, and any pending facility-change request for that shipment is automatically closed (`APPOINTMENT_CANCELLED`).
  - Inside the cut-off or when a customer penalty applies, an escalation case is opened for Operations (see Scenario **I.2** below).

### G. Languages (English, Hindi, Hinglish)

- Send `bhai meri gaadi 60 minute late hai` on Telegram → the bot replies in **Hinglish** with system-verified hold details.
- Send a Hindi (Devanagari) message → the bot replies in **Hindi**.

### H. Allocation, Warehouse Change & Cancellation Lifecycle (Driver + Warehouse Authority)

These scenarios demonstrate full Warehouse Authority and Admin controls on `/warehouse` (covered by `tests/test_lifecycle_scenarios.py`):

1. **Free a booked or missed no-show slot via the Dock Board cross (`×`):**
   - Open `/warehouse` → **Dock board** (always shows all 24 hourly columns `00:00`–`23:00`).
   - Click the **`×`** button in the top-right corner of any booked cell (works for both upcoming slots and past no-show slots where the truck never checked in; hidden when the truck is on-site or unloading).
   - Confirm the warning prompt (*"Free D... ...? The shipment stays and will need a new slot. The driver will be notified."*).
   - The slot turns **Free**, any live hold is cleared from conversation memory, the driver receives a Telegram/chat notice, and the **Shipments** tab shows **`Planned: No slot allocated | State: Confirmed (no slot)`**.
2. **Resolve an escalation with "Cancelled by operations":**
   - In **Operations → Cases**, claim a case and resolve it with **Cancelled by operations**.
   - The active appointment is cancelled and the dock slot is freed immediately (refused with `ORIGINAL_IN_PROGRESS` if the truck is already on-site or unloading).
   - Clicking **Reopen** on a resolved `Assigned a slot` or `Cancelled by operations` case warns that the previous slot change is already saved and will not be undone automatically.
3. **Change warehouse directly in Shipments & allocate a new slot:**
   - In **Shipments**, select a new non-partner warehouse on any active shipment not yet on-site and click **Change warehouse** (prompts for confirmation).
   - The old dock slot is freed immediately, the old gate pass is revoked without locking out an unconfirmed driver, any pending partner or facility-change requests are closed, stale `latest_eta_ts` is reset so earlier slots at the new warehouse can be allocated, and the driver is notified.
   - At the destination warehouse, open **Shipments** and use **Allocate slot** (filtered to compatible dock types such as `REEFER`/`HEAVY`, up to 200 upcoming slots, and prompts before replacing an existing booking). Allocating a slot books it, notifies the driver, and sends a fresh Dock Access Pass.
4. **Cancel a confirmed shipment (before arrival):**
   - In **Shipments**, click **Cancel** on any active shipment (waiting, locked, or confirmed — as long as the truck is not `AT_GATE`, `WAITING`, `IN_DOCK`, or `CHECKED_IN`). The shipment is marked `CANCELLED`, its slot is freed, and its pass is cancelled.
5. **Gate check-in (`/gate`):**
   - Sign in as a **Security** user on `/gate`, enter the driver's Dock Access Pass code, vehicle registration, and licence number to validate and check the truck in (see [dock-access-pass.md](dock-access-pass.md)).
   - Once checked in (`AT_GATE` / `IN_DOCK`), warehouse change, slot freeing, and cancellation are all strictly blocked until unloading completes.

### I. Warehouse exceptions (`/warehouse`)

On `/warehouse`, for your assigned facilities (an Admin sees all):

- **Block a dock** (Dock board tab, under the board): choose dock, type, from, optional to, and a reason, then **Check bookings**. The warning lists each affected booking and where it would move. **Block and move** applies that exact plan; if things changed meanwhile you are shown the updated list. A booking that fits nowhere is freed and its shipment waits for a slot. Trucks already inside are left alone.
- **Reopen** ends a dock block early. Bookings already moved stay where they are.
- **Write to a driver:** **Operations → Chats**, choose the driver, type and **Send**. **Resend** appears for any message whose delivery failed.
- **Delete / Restore a driver:** **Drivers** tab (lists unfinished shipments that will be cancelled and requires typing the driver's name). Admins can restore deleted drivers from `/admin` (**Deleted drivers**).

If a driver cannot be reached on Telegram after a slot is freed or moved, the screen says so so staff can phone them.

## 3. Verifying edge cases and automated proofs

Failures that require a broken database, concurrent writers, or prompt-injection attempts can be verified with the test suite and proof scripts (see [scenario-catalogue.md](scenario-catalogue.md)):

```powershell
python -m pytest -q
```

| Verification | Script | What it proves |
|---|---|---|
| Sign-in, rate limit, sessions, memory rebuild | `python scripts/show_phase3.py` | identity rules, rate limit, hold re-verified on return |
| ETA rules | `python scripts/show_eta_cases.py` | late-by, repair travel time, gate-in contradiction |
| Search and change detection | `python scripts/show_phase4.py` | incomplete evidence, FCFS, stale-offer detection |
| Index-level double-booking protection | `python scripts/show_phase5.py` | database rejects concurrent double-booking; expiry |
| Idempotency, rollback, cancellation, outbox | `python scripts/show_phase6.py` | retry gets same answer; mid-confirm crash rolls back |
| Prompt injection, outages, unbacked claims | `python scripts/show_phase8.py` | forged tool calls refused; leaked prompt blocked |
| Hand-off queue and coordinator CLI | `python scripts/show_phase9.py` | competing claims, resolution reaches the driver |
| Concurrency proof (about 8 minutes) | `python scripts/run_integrity_proof.py` | threads and processes racing for one slot |
| Replay of recorded conversations | `python scripts/replay_traces.py` | rule changes show up as diffs; evaluators score traces |

### Calling the signed driver webhook directly

```python
import hashlib, hmac, json, httpx

secret = b"your-webhook-secret"
body = json.dumps({"from": "+91 90000 10006", "message_id": "M-1", "text": "my truck is 60 minutes late"}).encode()
sig = hmac.new(secret, body, hashlib.sha256).hexdigest()
print(httpx.post("https://<your-production-host>/v1/driver/messages", content=body, headers={"x-signature": sig}).json())
```

Also verifiable: wrong signature (`401`), repeated `message_id` (`duplicate: true`), same `message_id` with different text (`409`), >2000-char text (`422`), unknown phone (`403`).

## 4. How to explain the code

| Audience question | Show | Read |
|---|---|---|
| "Can the model book things by itself?" | Scenario A step 3; `tools.py` `execute` | [walkthrough 5](code-walkthrough/05-model-boundary.md) |
| "What stops two drivers getting one slot?" | `show_phase5.py` part 3; migration 002 | [walkthrough 4](code-walkthrough/04-holds-confirm-cancel.md) |
| "What if the model lies?" | `show_phase8.py` parts 2 and 6 | `conversation._guard`, `language.claims_*` |
| "What if the database or model is down?" | `show_phase8.py` part 5 | [walkthrough 5](code-walkthrough/05-model-boundary.md), [6](code-walkthrough/06-escalation-and-api.md) |
| "How do you know it behaves?" | `replay_traces.py`, `run_integrity_proof.py` | [walkthrough 7](code-walkthrough/07-evaluation-and-tracing.md) |

## 5. Open operational thresholds

Operational policy thresholds that depend on business sign-off (data protection, escalation SLA, cancellation cut-off, and others) are tracked in [operations-questions.md](operations-questions.md) and [client-readout.md](client-readout.md).
