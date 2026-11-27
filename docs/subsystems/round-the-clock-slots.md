---
title: Round-the-clock hours and automatic slots
type: subsystem
created: 2026-11-27
updated: 2026-11-27
sources:
  - code: src/setuhaul/round_clock.py
  - code: src/setuhaul/capacity.py
  - code: src/setuhaul/jobs/expire_holds.py
  - code: src/setuhaul/api.py
verified_at: b5b462f
---

# Round-the-clock hours and automatic slots

Warehouses work day and night, and their dock slots are created ahead without anyone opening them day by day. Typed
availability on the Shipments tab still exists; both feed the same feasibility rules
([scheduling algorithm](../scheduling-algorithm.md)).

## What happens when

| When | What | Where |
|---|---|---|
| Server start | every facility set to 00:00-00:00 and `LAST_NEW_START_TIME` rules switched off | `src/setuhaul/round_clock.py:30`, called at `src/setuhaul/api.py:1044` |
| Staff save a crew number | stored, future automatic labour windows updated, slots created | `src/setuhaul/round_clock.py:95` |
| Start and each maintenance sweep | missing slots created, 14 days including today | `src/setuhaul/round_clock.py:84`, `src/setuhaul/jobs/expire_holds.py:132` |

Slots are one hour, one per active dock. Labour windows made here are marked `AUTO` and fill only the hours nothing else
describes (`src/setuhaul/round_clock.py:42`). Partner warehouses are left out.

> ⚠️ A crew number is never assumed. A warehouse with none gets no slots, so "no labour data means cannot offer" stays
> true.

> ⚠️ A closing time at or before the opening time means the next day (`src/setuhaul/capacity.py:280`). Without this,
> 00:00-00:00 is a zero-length day and every slot is outside hours.

> ⚠️ Start-up overwrites the opening hours of every facility and ignores database errors with only a warning. Hours
> typed into the database do not survive a restart.

> ⚠️ Only `create_app_from_env` opens the hours. The demo console and tests that build the app directly keep the
> supplied hours.

> ⚠️ Creation skips any hour that overlaps an existing slot on the dock, so repeating it adds nothing. Keep it that way:
> it runs on every sweep.
