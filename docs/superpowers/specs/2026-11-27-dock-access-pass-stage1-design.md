# Dock Access Pass, stage 1: pass and gate

Status: awaiting approval. Date: 2026-11-27. Source: the owner's "Dock Access Token (DAT)" instruction.

## Intent

Stop unauthorised drivers from using a warehouse's docks. A Dock Access Pass, not an order number, authorises a truck
visit. Order numbers stay as business information.

## What exists today

No parking, gate check-in or check-out, truck visit, security role or gate screen. `facility_checkins` holds supplied rows
that are read, never written. Order numbers authorise no physical access; on the unmerged shipment branch they only unlock
a shipment in chat. Dock slots, holds, rescheduling, roles, facility scope, audit, rate limits and Telegram delivery are
reused.

## Stages

| Stage | Content |
|---|---|
| **1 (this document)** | Issue, deliver and redeem the pass; security role and gate screen; check-in, unloading complete, check-out; revoke and reissue |
| 2 | Numbered parking slots, dock allocation on arrival, reassignment and dock maintenance |
| 3 | Exception workflows (unscheduled arrival, substitution), several shipments per visit, dashboards, diagrams and threat assessment for the whole |

## Approaches

| Approach | Trade-off | Choice |
|---|---|---|
| A. Random value, QR and typed code | Fast at the gate; new library and picture sending | Not now |
| B. Random value, code only | Nothing new to add; the guard types or pastes about 26 characters | **Chosen** |
| C. Self-contained signed token | Still needs the database to revoke or stop reuse | Rejected |

## Decided by you

Pass and gate first. A security guard checks passes. The bot sends the pass; the driver sends it back once per shipment.
The pass replaces the order-number check. Parking will use numbered slots (stage 2). Code only, no QR.

## The pass

- 128 random bits from `secrets`, written as `DAP-` and 26 base32 characters in groups. Case, spaces and hyphens are
  ignored. Only its SHA-256 hash is stored, unique.
- Bound to one visit, driver, vehicle registration, carrier and warehouse.
- Valid from the slot start minus the facility's `CHECKIN_EARLY_LIMIT_MIN` (60 if unset) to the slot end plus 4 hours.
- One live pass per visit. A reissue revokes the old one in the same transaction.

## States

- **Pass:** `ISSUED` → `VALIDATED` (gate checks passed) → `ACTIVE` (checked in) → `COMPLETED` (unloading done) →
  `CLOSED` (checked out). Also `EXPIRED`, `REVOKED`, `CANCELLED`.
- **Visit:** `EXPECTED` → `CHECKED_IN` → `OPERATIONS_COMPLETE` → `CHECKED_OUT`, or `CANCELLED`.
- Appointment states are unchanged. Anything not listed is refused. Every change checks a row version.

## Flows

**Issue and deliver.** Creating a shipment creates the visit and pass and sends the code to the driver on Telegram, in the
same request, because the code is never stored. If the driver is not linked or sending fails, the shipment shows "Pass not
delivered". Linking Telegram, or **Send new pass** on the warehouse screen, issues and sends a fresh one.

**Redeem in chat.** As the order check does now: the app reads the code before the model, confirms the shipment, and until
then the bot cannot show or book it. Five wrong codes lock it and open a case. The code is blanked from the message before
it is saved, logged, put in a case or sent to the model.

**At the gate.** A new `SECURITY` role, bound to facilities, with a `/gate` screen.
1. The guard enters the code, the vehicle registration seen on the truck and the driver's licence number.
2. The server checks the code, warehouse, time window, driver eligibility and both numbers. Nothing about the shipment is
   shown until all pass.
3. **Check in** activates the pass and records the visit in one transaction. A second check-in is refused.
4. After unloading, a warehouse user presses **Unloading complete**.
5. **Check out** (code or visit list) closes the visit. The code works for nothing afterwards.

**Changes.**

| Event | Effect |
|---|---|
| Reschedule at the same warehouse | Same pass and visit; the time window moves in the same transaction |
| Driver, vehicle or warehouse changes | Old pass revoked, new one issued and sent |
| Shipment cancelled | Pass and visit cancelled |
| Revoke (admin or that warehouse) | Reason required; effective at once |
| Window passes before check-in | `EXPIRED`; the warehouse sends a new pass |
| Window passes while the truck is inside | Nothing is released or closed; a case is opened for the warehouse |

## Refusals and limits

- The guard sees plain wording and what to do next: unknown or revoked pass, wrong warehouse, too early or too late,
  details do not match, already checked in, unloading not complete.
- An unknown code reads the same as a revoked one.
- Ten failed gate checks by one user in 15 minutes pause that user's checks for 15 minutes.
- Unscheduled trucks are refused and told to contact the warehouse. No override in this stage.

## Data

- New tables `truck_visits`, `visit_shipments` (one shipment each for now), `dock_access_authorizations` with the
  instruction's attributes. `parking_slot_id` waits for stage 2. SQLite text ids and UTC times.
- Migration `016` is still unmerged, so it is amended: `shipment_assignments` stays, now confirmed by the pass.
- A further migration rebuilds `users` to allow the `SECURITY` role. Take a backup before deploying.
- Check-in and check-out also set `gate_in_ts` and `gate_out_ts` in `facility_checkins`, so the existing arrival-time
  logic sees real arrivals. This changes the read-only rule for that table and is recorded as a decision.
- Order-number parsing and checking are removed. The order number stays on the shipment.
- Supplied, past and cancelled appointments get no pass.

## API

`/v1/...`, following existing conventions: gate validate, check-in and check-out; warehouse unloading-complete, send new
pass and revoke. Role and facility scope are checked on the server. Repeats of a successful action return the same result.

## Audit

Issue, delivery success or failure, redemption, each gate check and its reason, check-in, completion, check-out, revoke,
reissue and refused access: actor, time, request id and reason code. Never the code.

## Checks

Tests first, including: an order number alone does nothing; expired and revoked codes; each mismatch; double and
concurrent check-in; superseded code; nothing after check-out; the code absent from the database, logs, model prompts and
responses; another facility's guard; migration on a copy with existing users.

## Not met until later stages

From the instruction's acceptance list: no double dock allocation on arrival (stage 2; booking already prevents two
bookings of a slot), and several orders in one visit (stage 3).

## Risks

- The bot sends the code, so returning it in chat shows only possession of the linked Telegram account. Identity is checked
  at the gate.
- The code stays in the driver's Telegram history.
- The guard must compare the licence document with the person; the server only compares the number.

## My calls to check

The time window; four-hour grace; no gate override; checking the licence number and vehicle registration by typing them;
writing `facility_checkins`; the "Unloading complete" button in this stage; passes only for shipments created in the app.
