# SetuHaul rescheduling by chat: what it does, and what it does not yet do

For the head of operations and a warehouse planner. Prototype, 26 November 2026. Engineering detail:
[readiness-detail.md](readiness-detail.md).

**In short.** A late driver sends a message. The service works out the new arrival time, holds one suitable dock slot for
3 minutes, and books it only when the driver says yes. When it cannot be sure, it passes the case to a coordinator.
**It is not ready for real drivers.**

## Policy it follows
* **First to hold wins.** A higher-priority load does not take a slot another driver is already holding.
* **A slot is offered only if every check passes:** dock type, weight, cold chain, opening hours, dock faults, arrival time,
  unloading time, labour and the customer's deadline. Missing information means no offer.
* **Held is not booked.** The old appointment stays until the new one is confirmed.
* **Cancel by chat** only 120 minutes or more before the slot, with no penalty to the customer.
* **A person decides** when there is no slot, the records disagree, information is missing, a penalty is involved, or the
  driver asks for a person.

## What we have shown
* When many requests chased one slot, exactly one got it, in every repeated round. No double booking.
* A six-driver morning ran from start to finish, including three hand-offs to a coordinator.
These were run on test data with a scripted stand-in for the AI model, on one computer.

## Limits
* **Your data has no labour or customer-deadline records.** Until a feed exists, no slot can be offered automatically.
* Drivers are not yet told **why** an earlier slot went to someone else.
* No response time is promised on a hand-off.
* Loads needing longer than one slot (SHP1009, SHP1014) always go to a person.
* One facility's schedule is not re-planned as a whole.

## Open risks
1. The current flow has no recorded run with a real AI model, so its understanding of drivers is unmeasured.
2. Hindi and Hinglish wording has not been checked by a native speaker.
3. No real driver has used it. Privacy, retention and erasure of driver messages are undecided.
4. The database suits a prototype; it must be replaced before heavy or multi-server use.
5. Your current records already show 2 bookings over a dock's weight limit and 6 the truck can no longer meet. The service
   does not repair existing bookings.

## Decisions we need from you
1. Where do labour and customer-deadline data come from?
2. Should priority ever override first-to-hold?
3. Are the 3-minute hold and 120-minute cancellation limit right?
4. May a long unload run into the next slot?
5. May driver messages be sent to an outside AI provider, and on what terms?
