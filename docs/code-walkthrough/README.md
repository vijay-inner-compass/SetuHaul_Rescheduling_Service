# Code walkthrough

How to read, explain and demonstrate the SetuHaul Conversational Rescheduling Service.

Every source file also carries line-by-line comments. These documents give the **story above the lines**: what each
part is for, how a request travels through it, why it is built that way, and what can go wrong.

## The one-paragraph version

A truck driver messages that they are late. The service works out a new arrival time, finds dock slots that can really
take the truck, **holds** the first of them for 3 minutes ("No" moves to the next), and **confirms**
it when the driver says yes in a later message, ending the old appointment and telling the warehouse planner, the
customer and operations. A language model helps with understanding and questions, but it **never decides**: identity,
timing, capacity, approvals and every statement about a booking come from code and the database.

## Reading order

| # | Document | Layer | Source files |
|---|---|---|---|
| 1 | [Foundations](01-foundations.md) | time, settings, database, migrations, timestamps | `clock`, `config`, `db`, `schemas`, `migrations/*.sql` |
| 2 | [Identity, memory and limits](02-identity-memory-limits.md) | who is talking, what they said, how often | `identity`, `memory`, `rate_limit`, `observability` |
| 3 | [ETA, feasibility and options](03-eta-feasibility-options.md) | when will the truck arrive, which slots fit | `eta`, `capacity`, `feasibility`, `allocation`, `option_history`, `repositories` |
| 4 | [Holds, confirmation and cancellation](04-holds-confirm-cancel.md) | the transactional core | `booking`, `cancellation`, `events`, `notifications`, `integrity`, `jobs/*` |
| 5 | [The model boundary](05-model-boundary.md) | model loop, tools, prompt, wording, conversation | `model_gateway`, `tools`, `prompts`, `language`, `conversation`, `stub_model` |
| 6 | [Escalation and the HTTP API](06-escalation-and-api.md) | hand-offs and the outside world | `escalation`, `coordinator`, `api` |
| 7 | [Evaluation and tracing](07-evaluation-and-tracing.md) | proof the behaviour is right | `evaluation`, `langsmith_trace`, `proof`, `scale_fixture` |
| 8 | [The demonstration console](08-demo-console.md) | showing it all | `demo_ui`, `demo/index.html`, `scripts/run_demo.py` |

Companion documents:

* [Demonstration guide](../demonstration-guide.md): what to run, what to type, what you should see.
* [Scenario catalogue](../scenario-catalogue.md): edge cases and failure scenarios, each with the test that proves it.
* [Decision register](../decision-register.md): why each design choice was made (D1 onward).

## Five rules that explain most of the code

1. **The model never supplies identity.** No tool has a `driver_id` argument. The driver is found from the verified
   phone number. (`identity.py`, `tools.py`)
2. **Time is injected.** Nothing calls `datetime.now()`. Everything takes a `Clock`, so holds, rate limits and demos can
   be tested without waiting. (`clock.py`)
3. **The database is the referee.** A hold is an appointment row covered by the same unique index as a booking, so two
   people cannot hold or book one slot even if the application code is wrong. (`migrations/002`, `booking.py`)
4. **Facts come from committed rows.** "Held", "confirmed", "cancelled", held-slot and hand-off wording are
   rendered from the database in three languages. If the model's text claims something the database does not back, it is
   replaced. (`language.py`, `conversation.py`)
5. **Missing evidence is never assumed.** No labour data means "cannot check", not "assume available". (`feasibility.py`)

## How a message travels

```
driver message
  -> api.py        signature -> sender lookup -> duplicate check -> rate limit -> claim the message id
  -> conversation  save text -> language -> build prompt -> model loop
       model_gateway  (max 6 rounds, 4 tools, 30 s, cost cap)
         tools.py     allowed? valid arguments? quote in THIS message? one write only? -> handler
           eta / feasibility / option_history / booking / cancellation   (deterministic, transactional)
  -> conversation  system wording for committed facts (hold, confirm, hand-off) else guarded model text
  -> api.py        store the reply under the message id, return it
```

## How to explain the code to someone new

Start with the five rules, then follow one message through the diagram above using the
[demonstration guide](../demonstration-guide.md) so they see each step happen. Then open the walkthrough for the layer
they care about. Use the [scenario catalogue](../scenario-catalogue.md) to answer "what if...?".
