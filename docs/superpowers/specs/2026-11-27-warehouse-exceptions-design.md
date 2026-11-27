# Warehouse exceptions: block a dock, free a booked slot, delete a driver

Status: **awaiting approval**. Nothing described here has been built.

Lines marked *(proposed)* are choices made while writing and not yet discussed.

## Intent

Warehouse staff can deal with exceptions from the signed-in screens. Today a dock can only be blocked by writing to the
database, a booked slot cannot be freed from the dock board, and a driver cannot be removed.

## Goals

1. Block a dock for a period, moving the affected bookings to other docks.
2. Free a booked slot with a cross on the dock board.
3. Delete a driver, cancelling their unfinished shipments, with a warning first and a way back.

**Not included:** editing a block (reopen it and create a new one), moving bookings to another warehouse, removing
personal data, the demo console, Telegram commands for staff.

**Anti-goals:** a booking changed without the warning having shown it; a blocked dock that can still be booked; history
or audit rows edited or removed; a driver told something that is not stored.

## Approaches

| Approach | Trade-off |
|---|---|
| **A. Extend the existing dock events (chosen)** | Booking rules and board already read them, so "is this dock usable" has one answer. Reopening must target one block. |
| B. A separate table for screen-created blocks | Simpler to edit, but every availability check must read two sources; missing one lets a blocked dock be booked. |

All three features use one shared "free a booking" step.

## Shared: freeing a booking

For a `CONFIRMED` or `PENDING_CONFIRMATION` appointment, in one transaction:

- The appointment becomes `CANCELLED` and not current, with a reason naming the cause (slot freed, dock blocked,
  driver deleted).
- An appointment event records the staff user. The planner, customer and operations notices are queued as for a driver
  cancellation. An audit entry is written.
- Any hold the shipment has is released.
- The shipment is kept (except in driver deletion) and can be given a new slot.
- The gate pass must be refused while the shipment has no booking, and the driver must have a working pass again once
  it is rebooked.

**Never freed:** `IN_PROGRESS` appointments, and shipments whose truck is inside the gate.

**Telling the driver:** the message is queued for Telegram and added to the chat history, after the transaction commits
(`queue_driver_notice` commits on its own). It reaches the phone only if Telegram is configured and linked; the screen
says when the driver could not be reached *(proposed)*.

Admins can act at any facility, warehouse staff only at their own. Security users cannot. Partner warehouses are
excluded, as for shipment creation.

## 1. Block a dock

**Screen** (warehouse page, with the dock board): dock, from, to (optional: until reopened), reason (required). Below
it, current and upcoming blocks with **Reopen**. Times are India time. "To" must be after "from" and after now.

**Warning before saving.** Lists every booking on that dock whose slot overlaps the period and has not ended:

| Order | Driver | Booked now | Proposed new slot |
|---|---|---|---|
| (order) | (name) | dock and time | dock and time, or "none fits" |

Bookings that cannot be moved are listed with why. **Block and move** books each proposed slot and tells each driver the
new dock and time. Staff confirm; the driver is not asked first.

**Choosing the new slot:**

- Same warehouse, another dock, starting in the future, not blocked or taken.
- The same fit rules as warehouse shipment creation. A shipment with missing evidence therefore gets "none fits".
- Closest start to the original; ties go to the earlier one *(proposed)*.
- No slot is proposed twice. Shipments are placed in priority order, then original time *(proposed)*.
- None fits: the booking is still freed and the shipment waits for a slot. The warning says so.

Moving books the new slot as confirmed, records the old appointment as replaced and moves the gate pass with it.

**If things changed** between the warning and the confirmation (a slot taken, a new booking on the dock), nothing is
saved and the warning is shown again with the new list *(proposed)*.

Holds on the blocked slots are released and those drivers told.

**Storage.** A block is a `dock_status_events` row of type `MANUAL_BLOCK`, `MAINTENANCE` or `BREAKDOWN` (chosen on the
form *(proposed)*). A new companion table keyed by the event records who created it, and who reopened it and when.

**Reopen** ends that one block now (a block not yet started never takes effect). The event row is not changed or
deleted; availability treats the block's end as the reopening time. It does not use the `REOPENED` event type, which
closes every earlier block on the dock. Reopening also sets back to `OPEN` any not-yet-ended slots on that dock and
period that are stored as `BLOCKED` (the sample leveller and refrigeration blocks), unless another block still covers
them. Moved bookings are not moved back.

**Elsewhere:**

- The board shows a blocked slot with the block's reason (today an event block shows only "Blocked").
- The slot list for new shipments must not offer blocked slots.

## 2. Cross on the dock board

On the warehouse board, "Today by hour": a small **×** on each booked slot that has not started and can be freed. No
cross on held slots (they lapse), started slots, trucks inside the gate or unloading.

It is a real button with a label naming dock, time and order, and keeps working after the board's live refresh.
Confirmation: *"Free D2 10:00 for ORD-006 (name)? The shipment stays and will need a new slot."* It frees only; there
is no automatic move.

## 3. Delete a driver

**Who:** admins, any driver. Warehouse staff, only drivers they added, and not if the driver has unfinished shipments
at a facility outside their own *(proposed)*.

**Warning:** lists every unfinished shipment (not completed or cancelled) with its dock and time or "no slot", and
requires typing the driver's name.

**Refused** while any of their shipments is unloading or has its truck inside the gate.

**On confirm, all or nothing:**

- Each listed shipment is cancelled, its booking and holds freed, and its gate pass and expected visit cancelled.
- Pending facility-change and partner requests for those shipments are closed *(proposed)*.
- The driver is set inactive, their Telegram link revoked and their project allocations released. They lose chat access
  at once.
- They disappear from driver lists, shipment forms, project allocation and approval queues. Approving a deleted driver
  is refused.
- No message is sent to the driver *(proposed)*. Open hand-off cases stay for staff to close.
- A deletion record keeps who, when and the previous status. Past shipments and history keep the driver's name.

**Restore (admin only):** an admin sees deleted drivers and can restore one to the previous status. Cancelled shipments
and project allocations do not come back, and Telegram must be allowed to re-link *(proposed)*.

Adding a driver with a deleted driver's phone or licence number is refused with a message to restore them instead.

## Build order

Each step is usable on its own.

1. Shared freeing step and dock blocking.
2. The cross.
3. Driver deletion and restore.

## Checks

- A blocked dock is refused by driver chat options, holds, confirmation, and warehouse shipment creation.
- Reopening one block leaves other blocks on the same dock in force.
- Moves never double-book; a failure part-way saves nothing.
- On-site and unloading cases are left alone by all three features.
- Warehouse staff cannot act outside their facilities or drivers.
- After deletion the driver gets no chat access and cannot be assigned; after restore they can.
- Local checks only (`scripts/ci.py`); GitHub CI stays off.

## Left to the builder

Routes, table and column names, migration numbering, message wording (all supported languages) and test layout.
