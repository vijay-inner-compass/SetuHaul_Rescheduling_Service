# Shipment assignment with driver order-number confirmation

Status: awaiting approval. Date: 2026-11-27.

## Intent

SetuHaul has no screen or API for creating shipments, so a driver added through the app can never be served. A warehouse
user or admin should create a shipment on screen and assign it to a driver. The driver proves they hold the order by
sending its order number on Telegram. Only then may the bot show or book it.

## Goals

- Create and assign a shipment from the Warehouse screen.
- One confirmation per shipment, checked by the app, not by the AI model.
- An assigned and confirmed shipment can be rescheduled and booked end to end on today's date.

## Non-goals

- Bulk upload, or editing a shipment after the driver has confirmed it.
- Sending the driver the order number, or a message when a shipment is assigned. The driver gets the number from
  dispatch or their paperwork.
- Shipments to partner warehouses.
- Confirmation for the supplied sample shipments.

## Anti-goals

- The bot reveals an order number, or anything about an unconfirmed shipment.
- Data entered here is mistaken for supplied data, or missing evidence is filled in by assumption.

## Approaches

| Approach | Trade-off | Choice |
|---|---|---|
| A. The app matches the order number in the message before the model sees the shipment | The model cannot skip the check. The driver must type the number fairly exactly. | **Chosen** |
| B. The model reads the message and calls a verify tool | Copes with loose wording. Depends on model behaviour. | Rejected |

## Decided by you

- Create and assign (not only reassigning samples).
- Warehouse users for their own facilities; admins for any.
- One confirmation per shipment.
- Carried through to booking.
- Five wrong attempts lock confirmation and open a case for the warehouse.
- The shipment form includes a planned dock time. A second form opens availability.

## Screens

A new **Shipments** tab on the Warehouse page (admins use the same page). Every write is role-checked,
facility-scoped, same-origin and audited, like the existing warehouse writes.

**Open availability** (do this first)
- Fields: facility, first and last date (at most 14 days), docks, from and to time, slot length (default 60 minutes),
  crews, equipment available.
- Creates the missing `appointment_slots`, skipping ones that exist, and one `labour_windows` row per day with
  `source='WAREHOUSE_UI'`.
- A day that already has a labour window for that facility is left unchanged and reported on screen.

**New shipment**
- Order number, driver (eligible drivers the user can already see), vehicle (of the driver's carrier), origin name and
  city, destination facility, customer, product, weight, pallets, dock type, refrigerated, priority, expected unload
  minutes, planned departure, planned dock time (an open, unoccupied slot that fits the load), latest arrival
  (optional) and whether a penalty applies.
- Creates, in one transaction: the shipment (`ASSIGNED`, ETA = planned slot start), a `CONFIRMED` appointment with
  `booking_source='WAREHOUSE'`, a `consignee_commitments` row with `source='WAREHOUSE_UI'`, and the pending assignment.

**Shipment list**
- Order number, driver, destination, planned time, and state: Waiting for driver, Confirmed, or Locked.
- While not confirmed: **Change driver** and **Cancel** (shipment `CANCELLED`, its appointment cancelled).
- When locked: **Unlock** resets the attempt count.

## Order number

- Format: `ORD` followed by at least six digits, hyphens or spaces allowed, for example `ORD-261127-001`. Letter case,
  spaces and hyphens are ignored when matching, and it must be unique on that basis.
- The fixed format lets the app tell an attempted order number from other digits in a message, such as a time.

## Telegram behaviour

The check sits in the shared message pipeline, after duplicate detection and rate limiting and before the
conversation. It therefore applies to the HMAC channel too.

| Driver has a waiting shipment and sends | Result |
|---|---|
| Its order number | It becomes confirmed. Fixed reply naming the order, destination and planned time. The model is not called for that message. |
| An order number that matches none of their waiting shipments | Attempt counted. Fixed reply: it does not match. The reply is the same whether or not the number exists elsewhere. |
| A fifth wrong number | Their waiting shipments lock and a case opens. Fixed reply: contact the warehouse. |
| Anything else | The normal conversation, so breakdowns and requests for a person still work. A fixed line is added asking for the order number. |

- A message with several order numbers confirms every one that matches. Every different number that matches none of the
  driver's orders is one wrong attempt, and if they reach five nothing in that message confirms (changed after review).
- A locked shipment cannot be confirmed, even with the right number, until the warehouse unlocks it.
- The model is told only that a shipment is waiting for its order number: no identifier, number, destination or time.
- A number the driver has already confirmed is not an attempt; the message goes to the normal conversation.
- Both sides of a confirmation exchange are saved to the chat history, so the warehouse sees them.
- Fixed replies exist in English, Hindi and Hinglish.

## Data

Migration `016` (number to be checked when building) adds `shipment_assignments`: `shipment_id` (primary key),
`created_by`, `created_at`, `confirmed_at`, `failed_attempts`, `locked_at`.

- A shipment with no row is treated as confirmed, so supplied data behaves as today.
- One view, `v_driver_shipments`, holds the rule "no row, or confirmed". Every driver-facing read and ownership check
  uses it: the shipment list, `authorize_shipment`, conversation memory, cancellation, GPS pings and position handling,
  facility change and partner paths.
- Warehouse screens keep reading `shipments` and see unconfirmed ones.

## Rule change

The writable data contract says the app does not create shipments or slots. A new decision (next number, D100)
records that the Shipments tab may insert into `shipments`, `appointment_slots`, `appointments`, `labour_windows`,
`consignee_commitments`, and may update `shipments` and its appointment only to change driver or cancel while
unconfirmed. `docs/writable-data-contract.md` and the decision register are updated.

## Checks

- Tests first. For each entry point, an unconfirmed shipment is invisible and refused.
- Order matching: case, hyphens, spaces, a number inside a sentence, times and phone numbers not counted, five-attempt
  lock, unlock, replayed message not counted twice.
- Scope: a warehouse user cannot create for, list or change another facility's shipments.
- One end-to-end test on a date after the sample data: open availability, create, confirm, report a delay, hold, confirm.
  This also shows whether any facility rule or other supplied record blocks booking on current dates.

## Left to the builder

Module layout, route names, the escalation reason code, and exact wording.

## My calls to check

- The order-number format.
- Change driver and Cancel while unconfirmed.
- Availability limits (14 days, 60-minute default).
- Receiver information is a latest arrival time and a penalty flag, which is what the data model holds, not opening
  hours.
- The model is told that something is waiting, so it does not claim the driver has nothing.
