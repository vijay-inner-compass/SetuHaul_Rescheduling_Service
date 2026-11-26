# Problem Definition Sheet

## Problem
Drivers message SetuHaul when they run late, arrive early or break down. A planner then looks for a new dock slot by hand.
Many drivers write in the same half hour, usable slots are few, and two people can be promised the same one.

**How might SetuHaul** let drivers report a delay, ask questions and consider revised appointments by chat, while many
simultaneous requests meet limited warehouse capacity, without infeasible or conflicting promises?

## Who needs what
| Party | Knows | Needs from the service |
|---|---|---|
| Driver | location, breakdown, likely delay | clear choice, instructions, honest status |
| Dispatcher or carrier | vehicle and driver assignment | revised plan, escalation status |
| SetuHaul operations | plan, customer commitment, open exceptions | one view of many requests |
| Warehouse planner | docks, labour, local rules | feasible arrivals, no overbooking |
| Consignee | inventory or production dependency | reliable revised arrival |

## Service goal
1. Work out the arrival time from evidence, and ask only for what is missing.
2. Offer only slots that pass every hard rule.
3. Hold capacity safely and confirm only from committed state.
4. Hand over to a person, with the full record, whenever it cannot act safely.

The model reads English, Hindi and Hinglish and calls narrow tools. Plain code and the database decide identity, ETA,
feasibility, holds, confirmation and escalation.

## Facts, estimates, assumptions, unknowns

### Facts (stated in the brief, or counted in the snapshot)
* Brief: 280 to 360 loads a day, 6 facilities, 32 dock doors, 190 to 240 receiving appointments a day, 15 to 25 exception
  messages on a normal day, 20 to 35 within 30 minutes in a spike, 5 coordinators per shift, no live GPS feed.
* Snapshot of 4 August 2026: 2 facilities, 9 docks, 106 slots, 21 shipments, 15 drivers, 20 appointments.
* The database allows one active appointment per slot and one current active appointment per shipment.
* Every operational timestamp carries `+05:30`.

### Estimates (values that are somebody's prediction, and can be wrong)
* Declared ETAs (driver, warehouse or operations) and the planned ETA.
* Expected unload minutes per shipment.
* Reported delay minutes on an exception.
* Our load fixture for the scale test: 360 invented loads, not SetuHaul data.

### Assumptions (our defaults; none is approved by operations)
| Assumption | Default | Question |
|---|---|---|
| Hold lifetime | 3 minutes | Q1 |
| ETA buffer before a slot | 0 minutes | Q3 |
| Priority | advisory; never takes a held slot | Q4, Q5 |
| Cancellation | automatic only 120 minutes or more before the slot, with a known penalty-free commitment | Q6 |
| Unload time | must fit one slot | Q20 |
| "N minutes late" | counted from the planned ETA | D11 |

Full list: [operations-questions.md](operations-questions.md).

### Unknowns (the snapshot cannot tell us)
* Labour and equipment availability: no table holds it.
* Consignee latest-arrival times, penalties and contract terms.
* The real effect of a capacity reduction: no factor is recorded.
* Real message volume and timing, real model behaviour, real driver wording.
* Whether outcomes here would hold in operations: one day and 21 shipments prove nothing statistical.

See [snapshot-limitations.md](snapshot-limitations.md).

## What the data shows
Ten issues, each proved by a query: [data-issues.md](verification/data-issues.md). The most serious:

1. **Two active appointments exceed their dock's weight limit** (SHP1005 on D3, SHP1014 on D1).
2. **Six current appointments can no longer be met** by the latest ETA.
3. **No labour or consignee data exists**, so on the supplied data alone no otherwise feasible slot can be offered.

## Success measures
Zero conflicting allocations; zero cross-driver reads; every confirmation backed by a committed row; every hand-off
complete. Targets that need a real model or real traffic (clarification turns, share resolved without a person) are not
measured yet.

## Out of scope
National network optimisation, carrier selection and rates, driver-safety decisions, customs and hazardous-goods
workflows, penalty approval, and the optional facility-wide scheduling tool.
