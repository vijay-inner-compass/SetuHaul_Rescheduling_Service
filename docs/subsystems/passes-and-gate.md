---
title: Dock Access Pass and the gate
type: subsystem
created: 2026-11-27
updated: 2026-11-27
sources:
  - code: src/setuhaul/dock_pass.py
  - code: src/setuhaul/passes.py
  - code: src/setuhaul/pass_check.py
  - code: src/setuhaul/gate.py
  - code: src/setuhaul/api.py
verified_at: b5b462f
---

# Dock Access Pass and the gate

For shipments created on the warehouse screen, a pass authorises one truck visit. Daily use and the security table are
in [Dock Access Pass](../dock-access-pass.md); this page is how the code keeps it true.

## Pieces

| Piece | Where |
|---|---|
| Code format, hashing, finding and blanking codes | `src/setuhaul/dock_pass.py` |
| Issue, re-time, revoke, send | `src/setuhaul/passes.py:95` onwards |
| Driver sends the pass in chat | `src/setuhaul/pass_check.py:59`, called from `src/setuhaul/api.py:597` |
| Guard's check, check-in, unloading, check-out | `src/setuhaul/gate.py:90` onwards |
| Expiry and overstay sweep | `src/setuhaul/gate.py:280` |

The window runs from the facility's early-arrival limit (60 minutes if unset) before the slot to 4 hours after it ends
(`src/setuhaul/passes.py:37`). The app, not the model, decides whether a pass confirms a shipment; five wrong codes lock
it and open a case.

## Keeping the pass in step

Any change to a booking must keep the pass right:

- new appointment: `passes.sync_window` moves the window, and can revive an expired pass while the truck is still
  expected (`src/setuhaul/passes.py:129`);
- new warehouse: `passes.move_warehouse` revokes the code, and `send_pending` sends a new one from wherever there is a
  Telegram client (`src/setuhaul/passes.py:147`, `:167`);
- shipment cancelled: `passes.cancel`;
- truck on site: `passes.on_site` refuses holds, confirmation, cancellation and moves.

> ⚠️ A new way of changing an appointment must call `passes.sync_window` inside its transaction, or the pass keeps the
> old dock and time. Freeing a booking is the exception: the gate already refuses a shipment with no appointment.

> ⚠️ Only the hash of a code is stored. Blank codes out of any text before it is saved or sent to a model
> (`src/setuhaul/api.py:592`). The pattern is deliberately wider than a valid code.

> ⚠️ Send a pass outside a transaction: it talks to the network (`src/setuhaul/passes.py:234`).

> ⚠️ Only `PASS_NOT_VALID` and `DETAILS_DO_NOT_MATCH` count towards the guard's ten-failure pause
> (`src/setuhaul/gate.py:35`). Counting "too early" or a repeated press would close the gate to the next truck.

> ⚠️ "Trucks inside" is chosen by the visit's state, not the pass's (`src/setuhaul/gate.py:259`), so nothing done to a
> pass can hide a truck.
