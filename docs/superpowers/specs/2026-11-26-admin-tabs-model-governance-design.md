# Admin tabs and a report-only Model tab

Date: 2026-11-26. Status: awaiting approval.

## Intent

The Admin screen shows every section on one long page. Split it into tabs, like the Warehouse screen, and add a **Model** tab so an
admin can govern the AI model: which one is used, whether it is working, what it costs and how fast it answers.

## Goals

- Tabs: **Users**, **Pending drivers**, **Onboarded drivers**, **Audit log**, **Model**.
- The Model tab reports real figures recorded from each model call.

## Non-goals and anti-goals

- Report only: nothing on the screen changes a limit, a price or the model.
- No message text, reply text, prompt, driver identity or API key is stored or shown.
- Recording must never fail or noticeably delay a driver's message.
- No charts, export, alerts or automatic clean-up of old rows.
- Never show a cost of 0 when the price is simply not set.

## Approaches

| Approach | Trade-off | Decision |
|---|---|---|
| A. Record each model call | real response time, retries included; a new table and more rows | **Chosen**: only this measures the model's own response time |
| B. Record one total per driver message | fewer rows; hides the model's response time | Not chosen |
| C. Read from LangSmith | no new table; off by default, needs a key, sends data outside | Not chosen |

## Design

**Recording.** A new migration `014_model_calls.sql` adds a table `model_calls`: time, provider, model id, response time (ms),
outcome, input tokens, output tokens, estimated cost (empty when no price is set). A small wrapper around the model gateway times
each attempt, so a retry is its own row. Outcomes: `OK`, `TIMEOUT`, `TRANSIENT`, `REFUSED` (permanent error), `NOT_CONFIGURED`,
`ERROR`. The wrapper is applied where the conversation gets its gateway (`api.py`), which the web demo and the real service share.
Rows are saved after the turn; a failure to save is logged and ignored. The error passed to the conversation is unchanged.

**Model tab** (admin only):

- **In use:** provider and model id (or "No model configured"; the scripted stand-in is labelled as such).
- **Health:** the last call's outcome and time, and the failure rate.
- **Totals**, for the last 24 hours and for all recorded calls: calls, failures, typical (median) and 95th-percentile response time,
  input and output tokens, estimated cost.
- **Limits in force:** call timeout, retries, calls and tool runs per message, message deadline, cost budget per message, output
  length, prices per million tokens, and whether LangSmith tracing is on.
- **Recent calls:** the latest 50.

```
Users | Pending drivers | Onboarded drivers | Audit log | [Model]

In use: <provider> / <model id>          Health: last call OK
             Calls  Failed  Typical  95th   Tokens in/out   Est. cost
Last 24 h       42       3    1.8 s  5.1 s     40k / 11k    price not set
All recorded   120       4    1.7 s  5.0 s    110k / 30k    price not set
```

**Tabs.** The Warehouse tab behaviour is shared with Admin: one section at a time, the choice kept in the address. Filtering the
audit log, and **Clear filters**, stay on the Audit log tab. Existing actions and permissions do not change.

## Limits to know

- Figures start from when this ships; earlier calls were never saved.
- The web demo rebuilds its database at each start, so its history resets. Its clock is frozen, so "last 24 hours" covers the run.
- The demo and the Telegram server have separate databases, so each shows only its own calls.
- Cost is an estimate from `SETUHAUL_MODEL_PRICE_IN_PER_MTOK` and `..._OUT_PER_MTOK`, not the provider's bill.

## Tests

Wrapper: each outcome, a retry giving two rows, a failed save not breaking a reply, no text stored. Page: admin only, five tabs,
"price not set", empty state, correct totals from known rows. Tabs checked in a browser.

## Left to the builder

Module names, page layout details and how the shared tab script is arranged.
