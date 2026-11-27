---
title: Warehouse operations that change bookings
type: subsystem
created: 2026-11-27
updated: 2026-11-27
sources:
  - code: src/setuhaul/assign.py
  - code: src/setuhaul/release.py
  - code: src/setuhaul/dock_blocks.py
  - code: src/setuhaul/driver_messages.py
  - code: src/setuhaul/escalation.py
  - code: src/setuhaul/auth/driver_removal.py
  - code: src/setuhaul/auth/exception_routes.py
verified_at: b5b462f
---

# Warehouse operations that change bookings

What a person on the warehouse screen can do to a booking or a driver. The driver's own path is not here; the passes
these changes affect are in [[passes-and-gate]]. How to use the screen is in `src/setuhaul/auth/wiki_pages.py`.

## The four changes

| Action | Where | Booking effect |
|---|---|---|
| Resolve a case with a slot | `src/setuhaul/assign.py:102` | old appointment ends, new one `CONFIRMED` |
| Free one slot | `src/setuhaul/release.py:110` | appointment cancelled, shipment kept; not after the slot starts |
| Block a dock | `src/setuhaul/dock_blocks.py:282` | caught bookings move to another dock or are freed |
| Delete a driver | `src/setuhaul/auth/driver_removal.py:95` | unfinished shipments cancelled; restore does not bring them back |

All are one transaction each. Block and delete take the list the warning showed and refuse with `PLAN_CHANGED` if it is
no longer true.

- Case assignment judges the slot with `evaluate_slot` against the load's latest ETA, never before now or a fresh GPS
  floor (`src/setuhaul/assign.py:90`). `REQUIRES_APPROVAL` is accepted because Operations is the approval.
- A dock block proposes the slot on another dock closest in time to the original (`src/setuhaul/dock_blocks.py:199`),
  placing higher-priority loads first.

> ⚠️ `resolve_case` books a slot only when the caller passes `apply` (`src/setuhaul/escalation.py:344`). The web route
> does; the demo console and the coordinator CLI do not, so "Assigned a slot" there records the outcome and moves
> nothing.

> ⚠️ `truck_inside` (`src/setuhaul/release.py:68`) reads both the supplied shipment status and the gate's visit tables.
> Checking only one lets a booking be moved under a truck that is already there.

> ⚠️ In a dock block, every caught booking is freed before any is moved (`src/setuhaul/dock_blocks.py:303`). Keep the two
> loops separate: a move may need what another caught booking still occupies.

## Telling the driver

- Free and block tell the driver after the transaction commits (`src/setuhaul/release.py:128`). A failure is reported as
  "not reached" and never raised, because the change is already saved.
- Case claim and resolve are queued for the phone only when the case names a load (`src/setuhaul/escalation.py:332`).
- Operations can write at any time, and `Resend` re-queues only a message given up on
  (`src/setuhaul/driver_messages.py:35`, `:74`).
- Deleting a driver sends them nothing.

> ⚠️ Queue a driver's notice after the booking transaction, not inside it: queuing commits on its own.

> ⚠️ Pass `redis` when telling a driver, so the cached conversation holds the message too. Otherwise the chat model does
> not know what the driver was just told.
