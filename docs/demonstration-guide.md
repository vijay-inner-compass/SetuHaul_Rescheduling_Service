# Demonstration guide

How to show the whole service working, how to explain it, and how to show the edge cases and failures.

Use **synthetic data only**. The demo console runs on a simulated clock (4 Aug 2026, 09:35 IST) and a copy of the supplied
snapshot with labelled `DEMO_FIXTURE` evidence. Nothing real is touched.

## 0. Before you start

```powershell
python scripts/run_demo.py
```

This opens `http://127.0.0.1:8800/demo`. If no model is configured in `.env`, the assistant is the **rule-based stand-in**
(deterministic; it understands phrases such as "60 minutes late", "take the first", "confirm"). It is not a model and says
nothing about model quality. For realistic wording, configure a provider in `.env` and switch to **Live model**.

The page has three panels: **Driver console** (chat), **Dock board** (docks by hour) and **Operations** (cases, activity).
The top bar has the simulated clock, the assistant switch, **+1 min**, **+3 min and sweep** and **Reset demo**.

Press **Reset demo** between scenarios. The reset dialog offers **Supplied schedule** (the snapshot's existing bookings) or **Empty dock board** (every slot free, so you allocate from scratch). To start empty, run `python scripts/run_demo.py --fresh`.

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

You should see: no hold is placed. Activity shows only the planner's booking. With the rule-based assistant the wording is
generic ("Thanks, noted."), because the stand-in does not write explanations; the **tool result** is `SLOT_CONFLICT`/`OPTION_CHANGED`
and nothing is held. A live model explains it in words. The database enforced the result, not the model.

### D. No slot fits: hand-off to a person

1. Choose **Pradeep Jat** (order ORD-260804-014). Click **Running 60 minutes late**.
2. The driver is told the case was passed to operations, with a **case reference**, and that nothing on the booking changed.
3. In **Operations > Cases** the card appears: reason "No slot fits this load", the driver, the load, and recent messages.
4. Choose a coordinator and click **Claim**. The driver's chat gains an operations message saying a member of the operations team is now looking at the case.
5. Click **Resolve**, pick a code, write a note. The note appears in the driver's chat.

### E. Languages

* Type or click Hinglish phrases that include "NN minute ... late" (for example "bhai meri gaadi 60 minute late hai"): the menu and
  hold wording are rendered in **Hinglish** from the same stored data.
* The rule-based assistant does not understand Devanagari. For Hindi, use **Live model**, or run the scripted transcripts:
  `python scripts/show_phase8.py` (Hinglish and Hindi sections).
* Wording is a draft; a native speaker should review it (see the decision register).

### F. Reset

Click **Reset demo**: schedule, clock and chats return to the start.

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
