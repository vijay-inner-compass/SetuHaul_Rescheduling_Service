# Dock Access Pass (stage 1)

A Dock Access Pass, not an order number, authorises a truck visit. The design is in
`superpowers/specs/2026-11-27-dock-access-pass-stage1-design.md`.

## Set up

1. **Admin > Users:** invite each gate guard with the role **Security (gate)** and their warehouse.
2. The Telegram bot must be configured, and each driver linked to it.

## Daily use

| Who | Where | Does |
|---|---|---|
| Warehouse | **Warehouse > Shipments** | Opens availability, creates the shipment. The bot sends the driver the pass. |
| Driver | Telegram | Sends the pass back once; then can ask about and change that booking. |
| Guard | **Gate** | Types the pass, the vehicle number on the truck and the licence number. **Check pass**, then **Check in**. |
| Warehouse | **Warehouse > Shipments** | **Unloading complete** when the truck is unloaded. This frees the dock slot. |
| Guard | **Gate > Trucks inside** | **Check out**, or check out by pass. |

The pass works from the facility's early-arrival limit (60 minutes if unset) before the slot to 4 hours after it ends.

## When something goes wrong

| Situation | What to do |
|---|---|
| "Pass not delivered" | Ask the driver to link Telegram, or check the bot; then **Send new pass**. |
| Lost or shared pass | **Revoke pass** (with a reason), or **Send new pass**, which replaces it. The driver sends the new one back. |
| Too early or too late | The gate refuses. For a missed window, book again, then **Send new pass**. There is no gate override. |
| Details do not match | Do not admit. A different driver or vehicle needs the shipment changed by the warehouse. |
| Truck with no booking | Do not admit; the warehouse creates a shipment first. |
| Truck still inside after the window | A case opens for the warehouse. Nothing is released automatically. |
| Five wrong passes in chat | The shipment locks; **Unlock** on the Shipments tab. |
| Ten failed gate checks in 15 minutes | That user waits 15 minutes. |
| System unavailable | The gate refuses; contact the warehouse. |

A warehouse move revokes the pass at once and sends a new one; the gate admits the truck only once a slot at the new
warehouse is booked. A move to a partner warehouse leaves no pass.

## Security

| Threat | Control | What remains |
|---|---|---|
| Guessing a pass | 128 random bits; attempt limits in chat and at the gate | None practical |
| Database or backup read | Only the SHA-256 of each pass is stored | Licence numbers and phones are stored as before |
| Pass left in logs, chat history or the AI model | Codes are blanked before a message is saved or sent on | A code typed in an unrecognisable form is not blanked |
| Someone else uses a pass | The guard must also type the vehicle and licence numbers | The guard must compare the licence photo and the person |
| Replay or a second entry | One live pass per visit; check-in once; dead after check-out | None known |
| Stolen Telegram account | Revoke the Telegram link and send a new pass | The bot sends the pass to the linked account |
| Pass in the driver's Telegram history | Short time window; revoke or replace | The message stays on the phone |
| A guard acting alone | Every check, check-in and check-out is audited | No second approval |

## Deploying and going back

Take a backup first. Migration `016` adds tables; `017` rebuilds the `users` table to allow the Security role. To go
back, deploy the previous image **and restore that backup**: the previous version does not know the Security role. There is
no feature switch; passes exist only for shipments created on the Shipments tab, and supplied data is untouched.

## Not in this stage

Parking slots, dock allocation on arrival, several shipments per visit, an unscheduled-arrival workflow, dashboards and
QR codes.
