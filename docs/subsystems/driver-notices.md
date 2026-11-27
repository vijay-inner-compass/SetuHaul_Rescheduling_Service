---
title: Driver notices
type: subsystem
created: 2026-11-28
updated: 2026-11-28
sources:
  - code: src/setuhaul/events.py
  - code: src/setuhaul/jobs/send_driver_notifications.py
  - code: src/setuhaul/partner_booking.py
  - code: src/setuhaul/escalation.py
  - code: src/setuhaul/driver_messages.py
  - code: migrations/023_driver_notices_without_event.sql
verified_at: b6501b0
---

# Driver notices

A reply to a driver's own message is sent directly. Anything else the driver has to know goes into the
`notification_outbox` table and is sent to Telegram by the notice runner
(`src/setuhaul/jobs/send_driver_notifications.py:51`). Warehouse actions are described in [[warehouse-operations]].

## What is queued

Lapsed holds, a hold released because the truck is too far away, facility-change answers, closer-truck offers, partner
warehouse approval, decline, expiry and failure (`src/setuhaul/partner_booking.py:164`), case claimed and resolved
(`src/setuhaul/escalation.py:352`), Operations messages, warehouse moves and freed slots.

## Rules

> ⚠️ Inside a transaction call `add_driver_notice`; `queue_driver_notice` commits (`src/setuhaul/events.py:75`,
> `src/setuhaul/events.py:97`). Calling the second inside a booking would commit half of it.

> ⚠️ A new thing the driver did not ask for must be queued, not only written to the chat. The chat row alone never
> reaches the phone.

> ⚠️ A notice may have no appointment event (`event_id` is null: a case with no load, or a load never booked). Join
> `appointment_events` with a left join and take the load from `coalesce(e.shipment_id, o.shipment_id)`, or those
> notices vanish from the "not delivered" list (`src/setuhaul/driver_messages.py:55`). One with no load at all is visible
> to administrators only.

> ⚠️ A notice older than 30 minutes is marked `TOO_OLD` and never sent (`src/setuhaul/jobs/send_driver_notifications.py:23`).

Partner answers only arrive if the partner mail jobs run, and nothing in the deployed service runs them yet
([deployment guide](../deployment-hostinger.md)).
