# Warehouse screen: demo-style board with an Operations panel

Status: **layout approved** in conversation; implementation not started. Lines marked *(proposed)* were not discussed.

## Intent

The signed-in warehouse screen should read like the demo console: the whole day visible at once, with messages beside it.

## What changes (the Dock board tab)

- **Two columns:** dock board on the left, **Operations** panel on the right. On narrow screens the panel goes below.
- **Compact cells:** colour, pattern and the driver's initials. All of 08:00 to 21:00 fits at 1366 px and wider without
  sideways scrolling.
- **Detail on demand:** hovering shows driver, order and time. Clicking (or Enter) opens a small detail box with the same and,
  where allowed, **Free this slot** (with today's confirmation). The × moves there. Blocked cells show the reason.
- **Facility buttons** instead of the dropdown and Show *(proposed: more than five facilities fall back to the dropdown)*.
- **Operations panel** with three views: **Cases**, **Chats**, **Requests** (facility requests, partner requests, waiting for
  partner), each with a count. It replaces the **Messages** tab. Old `#messages` links and the attention cards open it.
- **Below the board**, folded: **On the bays now** and **Block a dock** with current blocks *(proposed)*.

Tabs become Dock board, Projects, Shipments, Drivers.

## Must keep

- Live refresh, including typed case notes, focus, chosen chat, open panel view and the stale-screen notice.
- Status never by colour alone; every cell reachable and named for keyboard and screen readers.
- Same actions, permissions and facility scope; same security headers, no inline script or style.

## Not included

The demo console (unchanged), its driver panel and planner booking, other screens, a shared board component between the demo
and the warehouse screen.

## Approaches

| Approach | Trade-off |
|---|---|
| **Demo-style with side panel (chosen)** | Board and messages together; more rework, and names need a hover or click. |
| Compact board only | Less change; messages stay on another tab. |
| Reuse the demo's page code | Closest match, but it is a client-rendered demo with inline script, which the signed-in screens forbid. |
