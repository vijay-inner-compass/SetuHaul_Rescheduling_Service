# GPS-based travel time and delay cross-check — design

Date: 2026-11-21. Status: awaiting approval.

## Intent and why

`derive_eta` (`src/setuhaul/eta.py`) trusts what the driver says. GPS pings let us:

1. **Cross-check driver-reported delays** — flag a report that disagrees with where the truck actually is.
2. **Fill in travel time** — when a driver reports a repair and gives no drive time, estimate it from position instead of asking.

## Goals

- Store truck position pings and facility coordinates.
- Estimate travel time from the latest fresh ping to the facility.
- Use that estimate as `travel_minutes` for `REPAIR` reports that lack one.
- For `LATE_BY` and `EXPLICIT_ETA`, compare the driver's ETA to the GPS ETA and mark a discrepancy.
- Accept pings through an API endpoint; seed demo pings for the console.

## Non-goals

- No routing or traffic provider (kept swappable; see Approaches).
- GPS never overrides the driver's ETA on its own.
- No live map UI, no ping history analytics.

## Anti-goals (failure even if it "works")

- A stopped, broken-down truck producing a confident GPS ETA that beats the driver's word.
- Stale or missing pings silently producing an ETA.
- Changing existing ETA behaviour when no GPS data exists: every current test must still pass unchanged.

## Constraints

- Existing result types: `EtaEvidence`, `ClarificationNeeded`, `EtaConflict`. `EtaEvidence.source` is currently `DRIVER_DECLARED | GATE_CHECKIN`.
- Gate check-in stays highest priority (it is a fact).
- Migrations are numbered SQL files; the next is `009`.
- The injectable `Clock` must be used for "now" and ping age (no wall-clock calls) so tests stay deterministic.
- Check `git status` before starting: other uncommitted edits (for example in `schemas.py`) must not be mixed into this work's commits.

## Approaches considered

- **A. GPS as a supporting signal — chosen.** New ping table and facility coordinates; a travel-time function; fills missing `travel_minutes`; flags (does not override) disagreements. Smallest change that fits the existing source/confidence/conflict model.
- **B. GPS authoritative.** Rejected: a truck stopped for repair still has a position, so a distance-based ETA would be wrong with high confidence, and it removes the driver from the loop.
- **C. Routing/traffic provider.** Rejected for now: external dependency, cost and failure handling. The travel-time function sits behind a small interface so it can be swapped in later.

## Design

**Data (migration `009_gps_pings.sql`)**
- `gps_pings(ping_id, shipment_id, lat, lon, recorded_at, received_at)`; index on `(shipment_id, recorded_at desc)`.
- Add `lat`, `lon` to `facilities`; seed values for the demo facility.

**Travel time (`src/setuhaul/travel.py`, new)**
- `estimate_travel_minutes(ping, facility_lat, facility_lon, *, avg_speed_kmph, road_factor) -> int`: haversine distance × `road_factor`, divided by speed, rounded up.
- Defaults (all in `config.py`, overridable): average speed 40 km/h, road factor 1.3 — **builder's call**, tune against demo data.
- Behind a `TravelTimeEstimator` protocol so a routing provider can replace it.

**Ping lookup**
- `load_latest_ping(conn, shipment_id, clock) -> GpsPing | None` returns the latest ping only if `clock.now() - recorded_at <= 15 minutes` (configurable, `gps_max_age_minutes`). Otherwise `None`, and behaviour is exactly as today.

**`derive_eta` changes** (new optional `gps_travel_minutes: int | None` argument; callers that omit it get today's behaviour)
- `REPAIR` with `travel_minutes is None`:
  - GPS travel time available → use it; ETA is `start + repair + gps_travel`; source `GPS_ASSISTED`, confidence capped at `MEDIUM`.
  - Not available → ask the driver, as today.
- `LATE_BY` / `EXPLICIT_ETA`: when a GPS ETA exists and `|driver ETA − GPS ETA| > 30 minutes` (configurable, `gps_mismatch_minutes`), return the driver's ETA with confidence lowered one level and a `gps_discrepancy_minutes` value on the evidence. The driver's ETA stays the ETA; the discrepancy is surfaced to the coordinator and used in the reply.
- GPS ETA means `clock.now() + gps_travel`.
- Gate check-in logic is untouched.

**Schemas**
- `EtaEvidence.source` gains `"GPS_ASSISTED"`; add `gps_discrepancy_minutes: int | None = None`.
- `GpsPing` model (lat/lon range-validated, aware timestamp).

**Wiring**
- `tools._eta_for` loads the facility and latest ping, computes `gps_travel_minutes`, passes it in; `_eta_payload` includes the discrepancy.
- New `POST /v1/gps/pings` in `api.py`: validates lat/lon and timestamp, requires the shipment to exist, stores the ping.
- Demo: see "Demonstration" below.

## Where pings come from

`POST /v1/gps/pings` is the single entry point; everything else only reads stored pings.

- **Production, first source (chosen): driver-shared location in the chat channel.** A thin adapter converts the channel's location message into a ping for the driver's active shipment. No new app or vendor needed. Weakness: depends on the driver sharing, and on the channel exposing location; a missing or stale ping falls back to asking the driver (already designed).
- **Later: fleet telematics/GPS vendor** posting to the same endpoint, with vehicle-to-shipment matching. Not built now.
- **Not chosen: a dedicated driver app** (build and distribution cost).
- **Demo:** the console's Send ping button simulates a sender and calls the same endpoint, so the demo exercises the real path.
- **Trust:** the endpoint verifies the same request signature as the driver webhook (`x-signature`), so only trusted senders can post pings.
- **Open for the builder:** whether the channel actually delivers live location or only one-off location pins; the adapter handles whichever it is.

## Demonstration (in the existing demo console)

**Seed data.** Each demo driver starts with a ping, so the scenarios work straight after a reset.

**Editable GPS panel, per driver.** The presenter edits the GPS details and presses **Send ping**; nothing is sent until then:
- `lat` and `lon` (number inputs, validated like the API: lat -90..90, lon -180..180).
- A "distance from dock" slider (km) as a shortcut: it fills `lat`/`lon` along a fixed bearing from the facility. Editing lat/lon directly updates the readout instead; the two stay in sync.
- `Ping age` (minutes, default 0): the ping's `recorded_at` is demo-clock now minus this value, which lets the presenter show a stale ping without waiting.
- **Send ping** posts to the real `POST /v1/gps/pings` (via a `/demo/api/gps` wrapper, as the other demo actions do), stamped with the demo clock. Errors from validation are shown in the panel.
- A live readout after each send: distance, estimated travel time, GPS ETA, ping age, and whether it is fresh or stale.

```
Driver: Ramesh (SH-1042)           Dock: Bay 3, 14:00 slot
Lat [19.2183]  Lon [72.9781]   Distance [----o-------] 60 km
Ping age [2] min                              [ Send ping ]
GPS: ~2h 00m away -> GPS ETA 16:00 (ping 2 min old, fresh)
```

**Scripted scenarios** (each followed by a chat message from the driver):

| Scenario | Driver says | GPS | Expected result |
|---|---|---|---|
| Cross-check | "20 minutes late" | about 120 km out (~4 h) | Driver ETA kept, confidence lowered, discrepancy over 30 min shown in the coordinator view and reply |
| Fill in travel time | "Breakdown, repair takes 45 minutes" (no drive time) | 30 km out | No travel-time question; ETA = repair + GPS travel, source `GPS_ASSISTED` |
| Stale ping | same repair message | ping age over 15 min | Falls back to asking for the drive time, as today |

**Explainability.** The activity feed logs GPS distance, ping age and the travel-time arithmetic, so a viewer can see why the ETA came out as it did.

## Testing

- Unit: haversine and rounding; stale ping ignored; threshold boundary (exactly 30 min is not a discrepancy, 31 is).
- `derive_eta`: every existing case unchanged with no GPS; repair-with-GPS fills travel; repair-without-GPS still asks; discrepancy lowers confidence but keeps the driver's ETA; gate check-in still wins.
- API: bad coordinates rejected, unknown shipment rejected.
- Demo wrapper: ping age is converted using the demo clock; invalid lat/lon returns a readable error; nothing is stored until Send ping.

## Left to the builder

Default speed and road factor values, ping retention, the exact discrepancy wording in driver replies and the coordinator view.
