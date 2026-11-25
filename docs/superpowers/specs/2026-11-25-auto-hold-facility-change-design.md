# Auto-hold the first free slot, and facility change with warehouse approval: design

Status: draft for approval (2026-11-25). Size: project (booking behaviour and data rules change).

## Why
A late driver has to read three near-identical dock options and pick a number, the replies do not say which warehouse they
are about, and there is no way to move a load to another facility.

## Decided with the owner
- The **first free slot is held** automatically; the driver is told facility, dock and time and confirms. Nothing is booked
  without consent. "No" releases it and holds the next one.
- A **driver asks** to change facility; a **warehouse user approves or rejects** before any slot is booked there.
- Allowed destinations: Jaipur, Gurugram **and the partner warehouse** (partner bookings keep the email approval).
- The driver sees only their **own booking card**, never the dock board.

## Goals
- One slot offered at a time, in plain words: facility, dock, date, time, how long it is held, and what to reply.
- Every reply about a slot names the facility.
- A facility change request is visible to the destination facility's warehouse users and in their "needs attention" strip.

## Non-goals
- Booking without driver confirmation. Warehouse- or admin-initiated facility changes. New facilities. Re-planning route or ETA
  beyond what the existing travel estimate gives for the new site.

## Design
- **Auto-hold:** when a slot is needed, the service ranks options as today, holds the first through the existing hold path and
  words it from stored state. "Yes" confirms through the existing confirm path. "No" releases the hold and holds the next
  unoffered option. No option left: the existing hand-off case. Hold expiry is unchanged. Deterministic Python decides;
  the model never picks or confirms.
- **Facility change request:** new table `facility_change_requests` (request_id, shipment_id, driver_id, from_facility_id,
  to_facility_id, status PENDING|APPROVED|REJECTED|EXPIRED, decided_by, timestamps). One open request per shipment. The driver
  asks in chat ("change to Gurugram"). The current booking stays until the new one is confirmed.
- **Approval:** a warehouse user of the **destination** facility approves or rejects on their screen (audited). Rejection tells
  the driver and changes nothing. Approval changes the load's destination, then auto-holds the first free slot there; the old
  appointment ends only in the same transaction that confirms the new one (existing rule).
- **Partner destination:** after warehouse approval, the existing partner email request is used; the driver is told it is
  waiting for the partner and is never told it is booked before the partner approves. A warehouse authority bound to the
  partner site (or an admin) is the approver.
- **Screens:** warehouse "Facility requests" list with Approve and Reject; driver card shows a pending request.

## Data rule that needs a decision
`shipments` is read-only supplied data. Moving a load needs one of:
1. **(Recommended)** Allow updating only `shipments.destination_facility_id`, only on an approved request, with the original
   kept in `facility_change_requests`. Scheduling, ETA and boards work unchanged.
2. Keep `shipments` untouched and add an override table. Every place that reads the destination must then consult it; easy
   to miss one and book at the wrong site.

## Approaches considered
- Hold-and-confirm (chosen), book immediately (rejected: drops consent), keep three options (rejected by owner).

## Testing
Hold/No/Yes sequence including "no options left" and expiry; one open request; only destination-facility users approve;
rejection changes nothing; the old booking survives until the new one is confirmed; partner path never claims booked early;
supplied shipment columns other than the destination never change.
