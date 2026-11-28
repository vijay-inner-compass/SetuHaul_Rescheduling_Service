# Demonstration guide

See also the [FAQ](faq.md) for the questions an audience is likely to ask.

How to show the whole service working, how to explain it, and how to show the edge cases and failures.

Use **synthetic data only**. The demo console runs on a simulated clock (4 Aug 2026, 09:35 IST) and a copy of the supplied
snapshot with labelled `DEMO_FIXTURE` evidence. Nothing real is touched.

## 0. Before you start

```powershell
python scripts/run_demo.py --auto-login
```

Open `http://127.0.0.1:8800/demo/login`. `--auto-login` has no authentication, so use it only on your own computer; without it the
demo shows the real sign-in page.

* **A model is required for the chat.** The demo uses the model configured in `.env` (`SETUHAUL_MODEL_PROVIDER`, `SETUHAUL_MODEL_ID`,
  `SETUHAUL_MODEL_API_KEY`); its name shows in the title bar. With none, the chat is off and the board and operations panel still work.
  What you type goes to that provider, so use made-up text only.
* **You type as the driver on a separate page.** The console's Driver panel is read-only. Open
  `http://127.0.0.1:8800/demo/driver-login` (Manoj Sharma; add `?driver=DRV007` for another) and keep the console beside it.
* For a demonstration through a phone, without a model, use [telegram-local-setup.md](telegram-local-setup.md).

The console shows a **Driver console** (conversation), a **Dock board** (docks by hour) and **Operations** (cases, activity, partner
mail). The top bar has the simulated clock, **+1 min**, **+3 min and sweep** and **Reset demo**. Press **Reset demo** between
scenarios; it offers the **Supplied schedule** or an **Empty dock board**.

## 1. Talk track (about 10 minutes)

1. **The problem.** A driver is late and the dock appointment no longer works. Today someone phones around.
2. **The rule.** The model understands and drafts; code decides what is allowed and what is true.
3. **Scenario A.** The driver was never asked who they are; one slot is held; nothing is booked until "yes".
4. **Scenario B.** The hold lapses and the original appointment is untouched.
5. **Scenario C.** A second driver is offered a different dock: held capacity is already taken.
6. **Scenario D.** No slot fits, so a person takes over with context.
7. **Scenario E.** Changing warehouse needs the destination's approval.
8. Close with the proof scripts in section 3 and say what is not proven (section 5).

## 2. Scenarios

The replies below were captured on 2026-11-26 with the scripted stand-in as the assistant. A real model may word its questions
differently; held, confirmed, released and hand-off wording is written by the system and reads the same. Times and docks depend on
the board and clock.

### A. Happy path: late, held, declined, confirmed

| Step | Type (driver page) | You should see |
|---|---|---|
| 1 | `my truck is 60 minutes late` | *I am holding 12:00-13:00 at dock D2 on 04 Aug 2026, SetuHaul Jaipur Distribution Centre, for you until 09:38. It is not booked
yet. Reply Yes to confirm, or No for the next slot.* The board cell shows **Held**. |
| 2 | `no` | That hold is released and the next is held: dock D4, 12:00-13:00. |
| 3 | `yes, please confirm` | *Your appointment is confirmed: dock D4, 12:00-13:00 on 04 Aug 2026. Reference APT-H-... Warehouse: SetuHaul Jaipur Distribution Centre.* |

Then open **Operations > Activity**: the history, and three queued notices (operations, customer, warehouse planner). **Deliver**
marks them sent; in this build nothing is really emailed or texted.

**What this proves:** identity from the channel, arrival time worked out from the planned time, feasibility, a transactional hold
and confirmation, immutable history.

### B. The hold lapses

1. `my truck is 60 minutes late`.
2. On the console press **+3 min and sweep**.
3. `yes, please confirm`.

You should see: *Your hold on that slot has lapsed. No booking has changed, and your original appointment still stands.*

### C. Held capacity is not offered to anyone else

1. As Manoj, `my truck is 60 minutes late` and do not confirm. Dock D2, 12:00 is held.
2. Open `http://127.0.0.1:8800/demo/driver-login?driver=DRV012` (**Arjun Das**) and send the same message.

You should see: Arjun is offered **dock D4**, not D2, with a note that the truck's GPS differs from the report. The database, not the
model, guarantees one holder per slot (`python scripts/show_phase5.py`).

### D. No slot fits: hand-off to a person

1. As **Pradeep Jat** (heavy load, `?driver=DRV014`), send `my truck is 60 minutes late`.
2. The driver is told the case was passed to operations, with a case reference, and that nothing changed.
3. In the console's **Operations > Cases**, **Claim** it as a coordinator, then **Resolve** with a code and a note. Both messages reach the driver's chat. (On `/warehouse` the outcome **Assigned a slot** also books a slot.)

### E. Change warehouse

1. As Manoj: `Please change facility to Gurugram`. Reply: the request is sent and the destination has **not** changed.
2. On the console's browser open `http://127.0.0.1:8800/warehouse`, then **Operations > Requests**, and approve the request. The `--auto-login`
   user can approve it; otherwise use an admin or a Gurugram warehouse user.
3. The driver's chat shows *The move to SetuHaul Gurugram Cross-Dock is approved. I am holding 16:00-17:00 at dock D1 ...* The hold
   lasts 15 minutes; confirm with `yes`.

### F. Keep or cancel

* `my truck is 60 minutes late`, then `never mind, keep my original slot`: *I have released the hold. Your original appointment is unchanged.*
* `please cancel my appointment`: with the demo data this goes to a person (case reference) and nothing is changed.

#### Example: release a held slot on the dock board

The demo console's board has no release button: a click on a free cell books it. A hold is released from the driver's side.

Press **Reset demo** (**Supplied schedule**), open `http://127.0.0.1:8800/demo/driver-login` (Manoj Sharma) and keep the
console's **Dock board** on SetuHaul Jaipur beside it.

| Step | Type (driver page) | Driver sees | Dock board shows |
|---|---|---|---|
| 1 | `my truck is 60 minutes late` | *I am holding 12:00-13:00 at dock D2 ...* | D2 12:00 **Held**; D4 10:00 still **Booked** (the original) |
| 2 | `never mind, keep my original slot` | *I have released the hold. Your original appointment is unchanged.* | D2 12:00 free again; D4 10:00 still **Booked** |

Other ways a hold ends:

* `no` releases it and holds the next slot (scenario A).
* **+3 min and sweep** lets it lapse (scenario B).

### G. Languages

`bhai meri gaadi 60 minute late hai` gets the held-slot message in Hinglish. Hindi in Devanagari depends on the configured model.
No native speaker has reviewed the wording.

### H. Empty dock board

Start with `python scripts/run_demo.py --fresh`, or **Reset demo** and choose **Empty dock board**. All supplied appointments are
removed, so a first booking has no "original ended" step. The hard rules still apply: Pradeep Jat still goes to a person.

### Warehouse exceptions (signed-in warehouse screen, not the demo console)

### I. Allocation, Warehouse Change & Cancellation Lifecycle (Driver + Warehouse Authority)

These scenarios demonstrate the full lifecycle across the **Dock board**, **Operations**, and **Shipments** tabs (covered by `tests/test_lifecycle_scenarios.py`):

0. **Create availability and a new shipment (`Shipments` tab):**
   - **1. Open availability:** Open `/warehouse` → **Shipments** → **1. Open availability**. Choose **Facility**, **First date** and **Last date** (up to 14 days), select one or more **Docks**, enter **From** and **To** times (e.g. `00:00` to `00:00` for all day), **Slot minutes** (default `60`), **Crews**, and **Equipment available** (`Yes`), then click **Open availability**. *(Alternatively, on the **Dock board** tab under **Round-the-clock slots**, save a **Crews** count to auto-create 24/7 1-hour slots for the next 14 days.)*
   - **2. New shipment:** In **Shipments** → **2. New shipment**, enter an **Order number** (`ORD` followed by at least 6 digits, e.g. `ORD-261129-001`), choose an eligible **Driver** and **Vehicle**, enter **Origin name**, **Origin city**, **Destination facility**, **Customer**, **Product**, **Load (kg)**, **Dock type** (`STANDARD`, `REEFER`, `HEAVY`, or `ANY`), **Refrigerated**, **Priority**, **Unload minutes**, **Planned departure** (before the slot start), and a **Planned dock slot**, then click **Create shipment**. The slot turns **Booked** on the Dock Board and a Dock Access Pass is sent to the driver to confirm.

1. **Free a booked or missed no-show slot via the Dock Board cross (`×`):**
   - On `/warehouse`, open the **Dock board** (always displays all 24 hourly columns `00:00`–`23:00`).
   - Click the **`×`** icon in the top-right corner of any booked cell (including past no-show slots where the truck never checked in).
   - Confirm the warning prompt. The slot turns **Free**, any live hold is cleared from conversation memory, the driver receives a cancellation notice in chat/Telegram, and the **Shipments** tab shows **`Planned: No slot allocated | State: Confirmed (no slot)`**.
2. **Resolve an escalation with "Cancelled by operations":**
   - In **Operations → Cases**, claim a case and resolve it with **Cancelled by operations**.
   - The active appointment is cancelled and the dock slot is freed immediately (refused if the truck is already on-site or unloading). Clicking **Reopen** warns that the previous slot change is already saved and will not be undone automatically.
3. **Change warehouse directly in Shipments & allocate a new slot:**
   - In **Shipments**, choose a new non-partner warehouse on any active shipment not yet on-site and click **Change warehouse** (confirms before proceeding).
   - The old dock slot is freed immediately, the old gate pass is revoked without locking out an unconfirmed driver, any pending partner/facility-change requests are closed, and the driver is notified.
   - At the new warehouse, open **Shipments** and use **Allocate slot** (automatically filtered to compatible dock types such as `REEFER`/`HEAVY`, up to 200 upcoming slots, and prompts before replacing an existing booking). Allocating a slot books it, notifies the driver, and sends a fresh Dock Access Pass.
4. **Cancel a confirmed shipment (before arrival):**
   - In **Shipments**, click **Cancel** on an active shipment (waiting, locked, or confirmed — as long as the truck is not `AT_GATE`/`IN_DOCK`/`CHECKED_IN`). The shipment is marked `CANCELLED`, its slot is freed, and its pass is cancelled.
5. **Driver chat cancellation closes pending facility changes:**
   - If a driver has a pending facility-change request and cancels their confirmed appointment in chat, the pending facility-change request is automatically rejected (`APPOINTMENT_CANCELLED`) so a late warehouse approval cannot move a cancelled booking.

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

Also on `/warehouse`:

* **Assigned a slot:** claim a case, choose this outcome and a slot, **Resolve**. The dock board changes and the driver is told.
* **Write to a driver:** **Operations > Chats**, choose the driver, type and **Send**. **Resend** appears for a message given up on.
* **Round-the-clock slots** (under the board): enter how many docks can unload at once; 14 days of slots are created.
* **Shipments and the gate:** create a shipment, then check its pass on `/gate` (needs Telegram for the driver to receive it):
  [dock-access-pass.md](dock-access-pass.md).

## 3. Showing the edge cases and failures

Many failures cannot be provoked from the page because they need a broken database, a slow model or two processes. Show them
with the evidence scripts and tests instead. The [scenario catalogue](scenario-catalogue.md) lists every case with its test.

| Show this | Run this | What it demonstrates |
|---|---|---|
| Sign-in, rate limit, sessions, memory rebuild after cache loss | `python scripts/show_phase3.py` | identity rules, 20 per minute limit, hold re-verified on return |
| ETA rules | `python scripts/show_eta_cases.py` | late-by, repair needs travel time, gate-in contradiction |
| Search and change detection | `python scripts/show_phase4.py` | missing evidence is incomplete, FCFS, an offer that goes stale |
| Index-level double-booking protection | `python scripts/show_phase5.py` | an outside writer is rejected by the database; expiry without sleeping |
| Idempotency, rollback, cancellation, outbox | `python scripts/show_phase6.py` | retry gets the same answer; a failure mid-confirmation rolls back |
| Prompt injection, outages, unbacked claims | `python scripts/show_phase8.py` | forged tool calls refused; leaked prompt blocked; model outage is safe |
| Hand-off queue and coordinator CLI | `python scripts/show_phase9.py` | competing claims, resolution reaches the driver |
| Whole flow, scored | `python scripts/run_scenario.py` | a scripted run with 23 checks |
| Concurrency (about 8 minutes) | `python scripts/run_integrity_proof.py` | threads and processes racing for one slot; invariants after every step |
| Replay of recorded conversations | `python scripts/replay_traces.py` | rule changes show up as differences; evaluators score every trace |
| Data quality of the supplied snapshot | `python scripts/audit_data.py` | read-only audit |

Run all tests:

```powershell
python -m pytest -q
```

### Calling the real webhook (not the demo)

The webhook needs a signature over the raw body. A minimal example in Python:

```python
import hashlib, hmac, json, httpx
secret = b"your-webhook-secret"
body = json.dumps({"from": "+91 90000 10006", "message_id": "M-1", "text": "my truck is 60 minutes late"}).encode()
sig = hmac.new(secret, body, hashlib.sha256).hexdigest()
print(httpx.post("http://127.0.0.1:8000/v1/driver/messages", content=body, headers={"x-signature": sig}).json())
```

Try also: a wrong signature (401), a repeated message id (same reply, `duplicate: true`), the same id with different text (409),
a 2001-character text (422), an unknown phone (403).

## 4. How to explain the code

| Audience question | Show | Read |
|---|---|---|
| "Can the model book things by itself?" | scenario A step 3; `tools.py` `execute` | [walkthrough 5](code-walkthrough/05-model-boundary.md) |
| "What stops two drivers getting one slot?" | `show_phase5.py` part 3; migration 002 | [walkthrough 4](code-walkthrough/04-holds-confirm-cancel.md) |
| "What if the model lies?" | `show_phase8.py` parts 2 and 6 | `conversation._guard`, `language.claims_*` |
| "What if the database or model is down?" | `show_phase8.py` part 5; tests in the catalogue | [walkthrough 5](code-walkthrough/05-model-boundary.md), [6](code-walkthrough/06-escalation-and-api.md) |
| "How do you know it behaves?" | `replay_traces.py`, `run_integrity_proof.py` | [walkthrough 7](code-walkthrough/07-evaluation-and-tracing.md) |
| "Where does a line of code come from?" | open the file | every source file has line-by-line comments |

## 5. Things the demo does not show

The operational thresholds that depend on a real model, real traffic or a real labour feed are **not** demonstrated by any scripted
run (see `client-readout.md`). Open operations approvals (data protection, SLA, cancellation cut-off and others) are listed in
`operations-questions.md`.
