# Warehouse chat visibility by facility

Status: **approach approved** in conversation ("record load per message"); implementation not started. Lines marked
*(proposed)* were not discussed. Replaces the project-based chat rule in `2026-11-24-auth-roles-design.md`.

## Intent

A warehouse authority sees the driver messages about loads going to their own facilities, whoever added the driver.

## Rule

- Each chat message records, when written, the load it is about and that load's destination facility at that moment
  *(proposed: so a later facility change does not hand old messages to the new warehouse)*.
- A warehouse authority sees messages recorded for one of their facilities. Nothing else.
- Admins see every message, as today.
- Messages with no recorded load are visible to admins only.

## Which load a message is about

1. A message the system writes about a load (booking changes, cases, partner answers, offers, passes): that load.
2. A driver's message and the reply: after the reply, the load selected in the conversation if still unfinished; otherwise
   the driver's only unfinished load; otherwise none. A message is never guessed between two loads.

## Older messages

Existing messages whose thread already names a shipment are recorded against it *(proposed; this covers the sample data
only, because live Telegram threads name none)*. All other older messages stay admin only.

## Screen and API

The Chats view and `GET /v1/warehouse/driver-chats` follow the rule. The note under "Driver chats" becomes: "You see
messages about loads going to your facilities. Messages not yet tied to a load are visible to administrators only."
Reading only; no change to who can write to a driver.

## Privacy

A warehouse authority gains messages from drivers they did not add, only for their facility's loads. A driver with loads at
two warehouses: each warehouse sees its own load's messages. A message that mentions the other load in passing is seen by
the warehouse of the load it was recorded against.

## Not included

Changing which drivers a warehouse authority can assign, phone numbers, writing to drivers.

## Approaches

| Approach | Trade-off |
|---|---|
| **Record load per message (picked)** | Precise; needs a table, and unclear or old messages are hidden. |
| Match by driver | Immediate, but shows messages about other warehouses. |
| Filter by the thread's shipment | Not workable: live threads have none. |
