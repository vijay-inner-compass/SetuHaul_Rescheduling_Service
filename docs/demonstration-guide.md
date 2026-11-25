# Demonstration guide

How to show the whole service working, how to explain it, and how to show the edge cases and failures.

Use **synthetic data only**. The demo console runs on a simulated clock (4 Aug 2026, 09:35 IST) and a copy of the supplied
snapshot with labelled `DEMO_FIXTURE` evidence. Nothing real is touched.

## 0. Before you start

```powershell
python scripts/run_demo.py
```

This opens `http://127.0.0.1:8800/demo`. The driver chat is answered by the **language model configured in `.env`**
(`SETUHAUL_MODEL_PROVIDER`, `SETUHAUL_MODEL_ID`, `SETUHAUL_MODEL_API_KEY`); its name shows in the title bar. There is **no
rule-based stand-in** any more. If no model is configured the page says so, the chat is switched off, and the dock board, planner
bookings and operations panel still work. Use synthetic messages only.

> **The Driver console is now read-only** (decision D90): driver messages come from Telegram. For scenario steps that say to type or send as the driver, use the demo driver account instead: `http://127.0.0.1:8800/demo/driver-login` (`?driver=DRV006` picks the driver). GPS pings cannot be sent from either page.

The page has three panels: **Driver console** (conversation, read only), **Dock board** (docks by hour) and **Operations** (cases, activity).
The top bar has the simulated clock, the assistant switch, **+1 min**, **+3 min and sweep** and **Reset demo**.

Press **Reset demo** between scenarios. The reset dialog offers **Supplied schedule** (the snapshot's existing bookings) or **Empty dock board** (every slot free, so you allocate from scratch). To start empty, run `python scripts/run_demo.py --fresh`. Scenarios A to F assume the supplied schedule; the empty-board walkthrough is scenario G.

## 1. Talk track (about 10 minutes)

1. **The problem.** A driver is late; their dock appointment may no longer work. Today a coordinator phones around.
2. **The rule.** The model understands and drafts; code decides what is allowed and what is true (see the
   [walkthrough README](code-walkthrough/README.md), five rules).
3. **Run scenario A** below. Point out: the driver was never asked for an id; the options are a numbered list from the database;
   "held" and "confirmed" were worded by the system.
4. **Run scenario B** (the hold lapses). Point out: nothing was promised that the database did not back.
5. **Run scenario C** (someone else takes the slot). Point out: the database, not the code, decides who wins.
6. **Run scenario D** (no slot fits). Point out: a person takes over, with context, and the driver is told in the chat.
7. **Close with the proof scripts** in section 4.

## 2. Scenarios on the demo console

### A. Happy path: late, options, hold, confirm

| Step | Do | You should see |
|---|---|---|
| 1 | Choose **Manoj Sharma** (order ORD-260804-006). Click **Running 60 minutes late** | A numbered list: "These slots are available. Nothing is held yet:" with lines such as "1. Dock D2, 12:00-13:00 on 04 Aug 2026" and a closing line "Reply with the number (1, 2, 3) to hold one." Nothing is held. |
| 2 | Click **Take the first option** | A **held slot** banner with a countdown. The dock board shows that cell as Held (amber, outlined as this driver's). |
| 3 | Click **Confirm it** | The assistant says the appointment is confirmed with dock, time and a reference, worded from the database. The board cell turns Booked. |
| 4 | Open **Operations > Activity** | History: held, original ended, confirmed. A **notification outbox** with three notices (planner, customer, operations). |
| 5 | Click **Deliver** | The outbox rows become Sent. |

**What this proves:** identity from the phone, ETA from the planned time plus the delay, feasibility, a stored menu, a
transactional hold, a transactional confirmation, immutable history, and downstream notices.

### B. The hold lapses

1. As in A steps 1 and 2 (stop before confirming).
2. Click **+3 min and sweep**. The banner disappears; Activity shows "hold ... lapsed".
3. Click **Confirm it**.

You should see: the driver is told the hold lapsed, **nothing changed and the original appointment still stands**, and the
slot is free again on the board.

### C. Someone else takes the slot

1. Choose **Manoj Sharma**, click **Running 60 minutes late** (menu shown, nothing held).
2. On the **Dock board**, click the cell for option 1 (Dock D2, 12:00) and book it as the planner. A message says whose load was booked.
3. Back in the chat click **Take the first option**.

You should see: no hold is placed. Activity shows only the planner's booking. The model explains it in its own words; the **tool result** underneath is `SLOT_CONFLICT`/`OPTION_CHANGED`
and nothing is held. The database enforced the result, not the model.

### D. No slot fits: hand-off to a person

1. Choose **Pradeep Jat** (order ORD-260804-014). Click **Running 60 minutes late**.
2. The driver is told the case was passed to operations, with a **case reference**, and that nothing on the booking changed.
3. In **Operations > Cases** the card appears: reason "No slot fits this load", the driver, the load, and recent messages.
4. Choose a coordinator and click **Claim**. The driver's chat gains an operations message saying a member of the operations team is now looking at the case.
5. Click **Resolve**, pick a code, write a note. The note appears in the driver's chat.

### E. Languages

* Type or click Hinglish phrases that include "NN minute ... late" (for example "bhai meri gaadi 60 minute late hai"): the menu and
  hold wording are rendered in **Hinglish** from the same stored data.
* Hindi (Devanagari) works as far as the configured model handles it; the scripted transcripts show the expected flow:
  `python scripts/show_phase8.py` (Hinglish and Hindi sections).
* Wording is a draft; a native speaker should review it (see the decision register).

### F. Reset

Click **Reset demo**: schedule, clock and chats return to the start.

### G. Fresh run: an empty dock board

Use this when you want to show allocation from scratch instead of a board that already has bookings. Start it with
`python scripts/run_demo.py --fresh`, or press **Reset demo** and choose **Empty dock board** (the choice sticks until you pick the other one).

**What is different from the supplied schedule**

* The supplied appointments are deleted, so every slot is Available (Jaipur: 69 Available, 10 Blocked, no Booked or Held) and no driver has an
  appointment. The driver card shows each load with no booking.
* Everything else is unchanged: the same drivers and loads, the same `DEMO_FIXTURE` labour and consignee evidence, the same 09:35 clock and GPS pings.
* A driver's first booking is a plain new booking. There is no "original appointment ended" step, because there was no original.
* The first menu is not the same as on the supplied schedule, because dock D1 is free: for Manoj Sharma it starts with D1 12:00, not D2.

| Step | Do | You should see |
|---|---|---|
| 1 | Reset with **Empty dock board** | Toast "Empty dock board ready." Dock board: no amber or blue cells. Driver card: loads show no appointment. |
| 2 | **Manoj Sharma**, click **Running 60 minutes late** | "These slots are available. Nothing is held yet:" with D1 12:00, D2 12:00, D4 12:00, then the closing line. Nothing is held. |
| 3 | Click **Take the first option** | "I am holding 12:00-13:00 at dock D1 ... until 09:38." The D1 12:00 cell turns Held. |
| 4 | Choose a second driver (for example **Arjun Das**), click **Running 60 minutes late** | Their menu does **not** include D1 12:00: D2 12:00, D4 12:00, D1 13:00. The held slot is already taken out of other drivers' offers. |
| 5 | Back to Manoj, click **Confirm it** | "Your appointment is confirmed: dock D1, 12:00-13:00 ... Reference APT-H-...". The cell turns Booked. |
| 6 | **Operations > Activity** | History has two lines, "held the 12:00 at dock D1 slot" and "appointment confirmed for 12:00 at dock D1". There is no "original ended" line. Three notices are queued (planner, consignee, operations). |
| 7 | **Pradeep Jat**, click **Running 60 minutes late** | Still a hand-off with a case reference: the heavy load fits no slot even on an empty board. Free capacity does not override the load rules. |

**What this proves:** allocation works with no existing schedule; capacity held by one driver is invisible to the next driver's search; and an empty
board does not weaken the hard rules (Pradeep Jat still goes to a person).

**Good for:** showing the first-come-first-served policy. Run steps 2 and 4 before either driver confirms and point out that each driver is offered only slots
nobody else holds.

The outputs above were captured on 2026-11-22 with the scripted test double as the assistant. A real model words its own explanations, but the numbered menu, hold,
confirmation and hand-off wording are system-worded and read the same. Slot choices can differ if the board or the clock differs.

## 3. Showing the edge cases and failures

Many failures cannot be provoked from the page because they need a broken database, a slow model or two processes. Show them
with the evidence scripts and tests instead. The [scenario catalogue](scenario-catalogue.md) lists every case with its test.

| Show this | Run this | What it demonstrates |
|---|---|---|
| Sign-in, rate limit, sessions, memory rebuild after cache loss | `python scripts/show_phase3.py` | identity rules, 20 per minute limit, hold re-verified on return |
| ETA rules | `python scripts/show_eta_cases.py` | late-by, repair needs travel time, gate-in contradiction |
| Search, menus, change detection | `python scripts/show_phase4.py` | missing evidence is incomplete, FCFS, a menu that goes stale |
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
| "Can the model book things by itself?" | scenario A step 2; `tools.py` `execute` | [walkthrough 5](code-walkthrough/05-model-boundary.md) |
| "What stops two drivers getting one slot?" | `show_phase5.py` part 3; migration 002 | [walkthrough 4](code-walkthrough/04-holds-confirm-cancel.md) |
| "What if the model lies?" | `show_phase8.py` parts 2 and 6 | `conversation._guard`, `language.claims_*` |
| "What if the database or model is down?" | `show_phase8.py` part 5; tests in the catalogue | [walkthrough 5](code-walkthrough/05-model-boundary.md), [6](code-walkthrough/06-escalation-and-api.md) |
| "How do you know it behaves?" | `replay_traces.py`, `run_integrity_proof.py` | [walkthrough 7](code-walkthrough/07-evaluation-and-tracing.md) |
| "Where does a line of code come from?" | open the file | every source file has line-by-line comments |

## 5. Things the demo does not show

The operational thresholds that depend on a real model, real traffic or a real labour feed are **not** demonstrated by any scripted
run (see `client-readout.md`). Open operations approvals (data protection, SLA, cancellation cut-off and others) are listed in
`operations-questions.md`.
