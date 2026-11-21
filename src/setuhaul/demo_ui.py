"""Demo screen: a localhost-only web page that drives the real service end to end.

NOT part of the production surface. It exists so people can watch the whole flow: a driver chatting, the dock board
changing as holds and bookings happen, and a coordinator working the escalation queue. Every driver message goes through
the same code path as the webhook (rate limit, duplicate handling, conversation, tools); only the signature step is skipped,
because the server stands in for the messaging channel. It runs on a fixed clock (the snapshot is dated 2026-08-04) with
buttons to move time forward, and refuses any request that does not come from the local machine.

The page shows people, not ids: drivers by name, loads by order reference and customer.
"""

# Lazy annotations.
from __future__ import annotations

# json decodes stored payloads and the service's responses.
import json

# os.urandom makes a throwaway webhook secret.
import os

# shutil removes a leftover folder.
import shutil

# sqlite3 for connections and constraint errors.
import sqlite3

# tempfile makes a scratch folder for the demo database.
import tempfile

# uuid makes ids for demo messages and planner bookings.
import uuid

# Callable typing.
from collections.abc import Callable

# dataclass for the demo state.
from dataclasses import dataclass, field

# Time types.
from datetime import datetime, timedelta

# Paths.
from pathlib import Path

# Typing helper.
from typing import Any

# Web framework types.
from fastapi import FastAPI, Request

# HTML and JSON responses.
from fastapi.responses import HTMLResponse, JSONResponse

# Request body models.
from pydantic import BaseModel, ConfigDict, Field

# Runs blocking database code in a worker thread.
from starlette.concurrency import run_in_threadpool

# The same facility snapshot the service uses, to draw the dock board.
# Releases or re-offers slots when a new ping arrives (same handling as the real API).
from setuhaul.booking import make_hold_checker

# The capacity snapshot
from setuhaul.capacity import build_facility_state

# A controllable clock.
from setuhaul.clock import FakeClock

# Settings loader (travel-time constants for the GPS readout).
from setuhaul.config import load_settings

# Database helpers.
from setuhaul.db import (
    IST,
    immediate,
    make_working_db,
    open_connection,
    parse_operational_timestamp,
)

# Case queue operations for the operations panel.
from setuhaul.escalation import claim_case, get_driver_case_status, list_open_cases, resolve_case

# GPS ping storage and the active-shipment lookup.
from setuhaul.gps import (
    active_shipment_for_driver,
    gps_travel_minutes_for,
    open_shipments_for_driver,
    store_ping,
)

# Maps phone numbers to drivers.
from setuhaul.identity import provision_identities

# The hold-expiry sweep, triggered by a button.
from setuhaul.jobs.expire_holds import expire_due_holds

# Conversation memory, used to write the driver's messages.
from setuhaul.memory import ConversationMemoryStore

# Delivers queued downstream notices when the button is pressed.
from setuhaul.notifications import deliver_pending_notifications

# A new truck position: confirmation, hold release, earlier-slot offers.
from setuhaul.position import handle_driver_position

# The validated ping type stored by store_ping.
from setuhaul.schemas import GpsPing

# Straight-line travel estimate and distance for the GPS readout.
from setuhaul.travel import StraightLineEstimator, haversine_km

# The demo's starting moment: the snapshot is dated 4 Aug 2026.
DEMO_START = "2026-08-04T09:35:00+05:30"
MESSAGE_ADVANCE_SECONDS = 20  # each driver message takes a moment, so holds can lapse during a demo
# The only client addresses the demo accepts.
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
SPARE_SHIPMENTS = ("SHP1018", "SHP1019", "SHP1008", "SHP1001")  # loads the planner can book without touching a driver in the demo
# Plain-language labels for hand-off reason codes, shown to the coordinator instead of raw codes.
REASON_TEXT = {
    "NO_SLOT": "No slot fits this load", "CONTRADICTORY_DATA": "Driver's report contradicts the gate record",
    "INCOMPLETE_FEASIBILITY_DATA": "Cannot check labour or customer data", "APPROVAL_REQUIRED": "Customer commitment needs approval",
    "SAFETY": "Safety concern", "REGULATED_LOAD": "Regulated load", "POLICY_CONFLICT": "Policy conflict",
    "DATABASE_UNAVAILABLE": "System unavailable during the conversation", "MODEL_UNAVAILABLE": "The assistant could not answer",
    "MODEL_LOOP_LIMIT": "The assistant hit its safety limit", "LANGUAGE_UNSUPPORTED": "Unsupported language",
    "CANCELLATION_CUTOFF": "Cancellation too close to the slot", "CUSTOMER_COMMITMENT": "Customer commitment at stake",
    "CUSTOMER_COMMITMENT_UNKNOWN": "Customer commitment unknown", "DRIVER_REQUESTED": "Driver asked for a person",
    "OTHER_UNCERTAINTY": "The assistant was unsure",
}


@dataclass
# Mutable demo state shared by all demo routes.
class DemoState:
    # The simulated clock the buttons move.
    clock: FakeClock
    # The demo's own database copy.
    db_path: Path
    # Rebuilds the database, cache and clock.
    reset: Callable[..., None]
    # True: the dock board starts empty (no existing appointments), so every slot can be allocated from scratch.
    fresh: bool = False
    live_factory: Callable[[], Any] | None = None  # the configured model gateway; without one, chat is switched off
    # The configured model's name, shown in the title bar (empty when none is configured).
    model_name: str = ""
    # Names offered in the operations panel.
    coordinators: tuple[str, ...] = ("Asha Verma", "Ben Thomas")
    # Free-form scratch space.
    notes: dict[str, Any] = field(default_factory=dict)


# A chat message typed in the driver console.
class ChatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    driver_id: str
    text: str = Field(min_length=1, max_length=2000)


# A request to move time forward (1 second to 2 hours), optionally running the hold-expiry sweep.
class ClockIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seconds: int = Field(ge=1, le=7200)
    sweep: bool = False


# A slot chosen on the dock board.
class SlotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot_id: str


# A reset request. fresh=True starts an empty dock board, fresh=False the supplied schedule, and leaving it out keeps the current mode.
class ResetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fresh: bool | None = None


# A coordinator claiming a case.
class ClaimIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coordinator: str


# A coordinator resolving a case.
class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coordinator: str
    resolution_code: str
    note: str = Field(max_length=500)


# A GPS position sent from the demo console, with how old the fix should look.
class GpsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    driver_id: str
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    age_minutes: int = Field(ge=0, le=1440)


def seed_demo_evidence(conn: sqlite3.Connection) -> None:
    """Labour and consignee evidence the snapshot lacks, labelled DEMO_FIXTURE, plus a clean chat history."""
    # The demo day.
    day = "2026-08-04"
    # Labour windows (8 crews, equipment available, 06:00-22:00) for both facilities, marked DEMO_FIXTURE so they are never mistaken for real data.
    for fac in ("FAC-JAI-01", "FAC-GGN-01"):
        conn.execute("insert into labour_windows (facility_id, window_start, window_end, available_crews, equipment_available, source, recorded_at) "
                     "values (?,?,?,8,1,'DEMO_FIXTURE',?)", (fac, f"{day}T06:00:00+05:30", f"{day}T22:00:00+05:30", f"{day}T06:00:00+05:30"))
    # A 'no deadline, no penalty' customer commitment for every shipment, also marked DEMO_FIXTURE.
    for (sid,) in conn.execute("select shipment_id from shipments").fetchall():
        conn.execute("insert or replace into consignee_commitments (shipment_id, latest_arrival, penalty_applies, exceptional, source, recorded_at) "
                     "values (?, NULL, 0, 0, 'DEMO_FIXTURE', ?)", (sid, f"{day}T06:00:00+05:30"))
    # A fresh GPS ping for each active demo driver's active load, 60 km straight north of the destination dock.
    seed_clock = FakeClock(parse_operational_timestamp(DEMO_START))
    # The same drivers the driver picker lists: active, and not generated test drivers.
    for (did,) in conn.execute("select driver_id from drivers where driver_status='ACTIVE' and driver_id not like 'GEN-%'").fetchall():
        # The load being driven, if any.
        sid = active_shipment_for_driver(conn, did)
        if sid is None:
            continue
        # The load's destination facility.
        fac = conn.execute("select f.lat, f.lon from shipments s join facilities f on f.facility_id=s.destination_facility_id where s.shipment_id=?", (sid,)).fetchone()
        if fac is None or fac["lat"] is None or fac["lon"] is None:
            continue
        # One degree of latitude is about 111.2 km, so 60 km north adds 60/111.2 degrees.
        store_ping(conn, sid, GpsPing(lat=fac["lat"] + 60 / 111.2, lon=fac["lon"], recorded_at=seed_clock.now()), seed_clock, "DEMO_FIXTURE")
    conn.execute("delete from chat_messages")  # the demo starts every conversation from nothing
    # Save the seed rows.
    conn.commit()
    # Map every driver's phone to their id, as production provisioning would.
    provision_identities(conn)


# Delete a file if it can be deleted; a file another connection still holds open (Windows) is simply left behind.
def _try_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# Create a fresh demo database.
def build_demo_db(path: Path, fresh: bool = False) -> None:
    # Delete any old database and its WAL files, if they can be deleted (Windows refuses while another connection has them open).
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    # Copy the seed database and apply migrations.
    make_working_db(path)
    # Open it to seed demo evidence.
    conn = open_connection(path=path)
    try:
        seed_demo_evidence(conn)
        # An empty dock board: remove the supplied appointments, so every slot is available and loads have no booking yet.
        if fresh:
            # Warehouse messages that pointed at an appointment keep their text but lose the link (the column allows empty).
            conn.execute("update operational_messages set appointment_id = NULL")
            # An appointment may point at the one it replaced; clear those links first, then delete the rows.
            conn.execute("update appointments set replaced_appointment_id = NULL")
            conn.execute("delete from appointments")
            conn.commit()
    finally:
        conn.close()


# Format a stored timestamp as HH:MM in IST.
def _hhmm(raw: str) -> str:
    return f"{parse_operational_timestamp(raw).astimezone(IST):%H:%M}"


# Format a time as HH:MM:SS in IST.
def _hhmmss(dt: datetime) -> str:
    return f"{dt.astimezone(IST):%H:%M:%S}"


# Attach the demo page and its API routes to the app.
def register_demo(app: FastAPI, deps: Any, process: Callable[[Any, str], JSONResponse]) -> None:
    # The demo state.
    demo: DemoState = deps.demo
    # The page is a single HTML file shipped with the package.
    html = (Path(__file__).parent / "demo" / "index.html").read_text(encoding="utf-8")

    # Refuse any request that does not come from this machine.
    def local_only(request: Request) -> JSONResponse | None:
        # The caller's address.
        host = request.client.host if request.client else ""
        # Remote callers get 403 DEMO_IS_LOCAL_ONLY.
        if host not in LOCAL_HOSTS:
            return JSONResponse({"status": "REJECTED", "reason_code": "DEMO_IS_LOCAL_ONLY"}, status_code=403)
        # Local caller: allowed.
        return None

    # Open a database connection (read-only for display routes).
    def db(readonly: bool = False) -> sqlite3.Connection:
        return open_connection(path=demo.db_path, readonly=readonly)

    # Map each shipment to 'driver name . order reference', so the page never shows ids.
    def names(conn: sqlite3.Connection) -> dict[str, str]:
        return {r["shipment_id"]: f"{r['driver_name']} · {r['order_reference']}" for r in conn.execute(
            "select s.shipment_id, s.order_reference, d.driver_name from shipments s join drivers d using(driver_id)")}

    # The simulated time as ISO text and a readable label.
    def clock_info() -> dict[str, Any]:
        # Simulated now.
        now = demo.clock.now()
        return {"iso": now.isoformat(), "text": f"{now.astimezone(IST):%a %d %b %Y, %H:%M:%S} IST"}

    # The demo page.
    @app.get("/demo", response_class=HTMLResponse)
    async def page(request: Request) -> Any:
        return local_only(request) or HTMLResponse(html)

    # Static configuration for the page: clock, model mode, coordinators, facilities.
    @app.get("/demo/api/config")
    async def config(request: Request) -> Any:
        def read() -> dict[str, Any]:
            conn = db(True)
            try:
                facilities = [{"id": r["facility_id"], "name": r["facility_name"]} for r in conn.execute("select * from facilities order by facility_name")]
            finally:
                conn.close()
            return {"clock": clock_info(), "chat_enabled": demo.live_factory is not None, "model": demo.model_name,
                    "coordinators": list(demo.coordinators), "facilities": facilities, "message_advance_seconds": MESSAGE_ADVANCE_SECONDS, "fresh": demo.fresh}

        return local_only(request) or await run_in_threadpool(read)

    # The driver picker: each driver with their active loads, customers and appointments.
    @app.get("/demo/api/drivers")
    async def drivers(request: Request) -> Any:
        def read() -> dict[str, Any]:
            conn = db(True)
            try:
                out = []
                # Active drivers only; generated test drivers (GEN-) are hidden.
                for d in conn.execute("select d.driver_id, d.driver_name, d.home_base_city, ca.carrier_name from drivers d "
                                      "join carriers ca using(carrier_id) where d.driver_status='ACTIVE' and d.driver_id not like 'GEN-%' order by d.driver_name"):
                    ships = []
                    # Each driver's unfinished shipments with ETA and current appointment.
                    for s in conn.execute(
                            "select s.shipment_id, s.order_reference, s.customer_name, s.current_status, f.facility_name, v.registration_number, "
                            "le.effective_eta_ts, sl.slot_start_ts, dk.dock_code from shipments s join facilities f on f.facility_id=s.destination_facility_id "
                            "join vehicles v using(vehicle_id) join v_latest_eta le using(shipment_id) left join appointments a on a.shipment_id=s.shipment_id "
                            "and a.is_current=1 and a.appointment_status in ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS') "
                            "left join appointment_slots sl on sl.slot_id=a.slot_id left join docks dk on dk.dock_id=sl.dock_id "
                            "where s.driver_id=? and s.current_status not in ('COMPLETED','CANCELLED') order by s.shipment_id", (d["driver_id"],)):
                        # Shape one load for display.
                        ships.append({"order": s["order_reference"], "customer": s["customer_name"], "status": s["current_status"].replace("_", " ").title(),
                                      "destination": s["facility_name"], "truck": s["registration_number"], "eta": _hhmm(s["effective_eta_ts"]),
                                      "appointment": f"{_hhmm(s['slot_start_ts'])} at dock {s['dock_code']}" if s["slot_start_ts"] else None})
                    # One driver card.
                    out.append({"id": d["driver_id"], "name": d["driver_name"], "city": d["home_base_city"], "carrier": d["carrier_name"], "shipments": ships})
                return {"drivers": out}
            finally:
                conn.close()

        return local_only(request) or await run_in_threadpool(read)

    # A chat message from the driver console.
    @app.post("/demo/api/chat")
    async def chat(request: Request, body: ChatIn) -> Any:
        denied = local_only(request)
        if denied:
            return denied
        # No model configured: the driver chat is switched off (there is no stand-in assistant), and nothing is created.
        if demo.live_factory is None:
            return JSONResponse({"http_status": 409, "status": "REJECTED", "reason_code": "NO_MODEL_CONFIGURED"}, status_code=409)

        def go() -> dict[str, Any]:
            # Imported here to avoid a circular import.
            from setuhaul.api import MessageIn

            conn = db(True)
            try:
                # The driver's phone number: the same identity a real webhook would carry.
                row = conn.execute("select phone from drivers where driver_id=?", (body.driver_id,)).fetchone()
            finally:
                conn.close()
            # Unknown driver.
            if row is None:
                return {"http_status": 404, "status": "REJECTED", "reason_code": "UNKNOWN_DRIVER"}
            # Build the same payload the webhook receives.
            payload = MessageIn.model_validate({"from": row["phone"], "message_id": f"DEMO-{uuid.uuid4().hex[:12]}", "text": body.text})
            # Run it through the REAL pipeline: duplicate check, rate limit, conversation, tools. Only the signature step is skipped.
            resp = process(payload, request.state.request_id)
            # Decode the service's JSON response.
            data = json.loads(bytes(resp.body))
            # Each message takes 20 simulated seconds, so a 180-second hold can lapse during a demo.
            demo.clock.advance(timedelta(seconds=MESSAGE_ADVANCE_SECONDS))
            return {"http_status": resp.status_code, **data, "clock": clock_info()}

        return await run_in_threadpool(go)

    # Everything the driver console shows: chat, live hold with seconds left, and case banner.
    @app.get("/demo/api/driver/{driver_id}/state")
    async def driver_state(request: Request, driver_id: str) -> Any:
        def read() -> dict[str, Any]:
            conn = db(True)
            try:
                # The driver's current conversation thread.
                thread = conn.execute("select thread_id from chat_threads where driver_id=? and thread_status<>'CLOSED' order by opened_at desc, rowid desc limit 1",
                                      (driver_id,)).fetchone()
                # Messages shown in the chat.
                msgs = []
                if thread:
                    for m in conn.execute("select sender_type, message_text, message_ts from chat_messages where thread_id=? and is_duplicate=0 "
                                          "order by rowid desc limit 40", (thread["thread_id"],)):
                        # Operations messages are labelled as such; the assistant's own replies as 'assistant'.
                        who = {"DRIVER": "driver", "AGENT": "assistant"}.get(m["sender_type"], "operations")
                        msgs.append({"who": who, "text": m["message_text"], "time": _hhmmss(parse_operational_timestamp(m["message_ts"]))})
                    # Newest were fetched first; show oldest first.
                    msgs.reverse()
                # The driver's hold, if any.
                hold = conn.execute(
                    "select a.appointment_id, sl.slot_start_ts, sl.slot_end_ts, dk.dock_code, m.expires_epoch from appointments a "
                    "join appointment_hold_meta m using(appointment_id) join appointment_slots sl using(slot_id) join docks dk on dk.dock_id=sl.dock_id "
                    "where m.driver_id=? and a.appointment_status='HELD'", (driver_id,)).fetchone()
                # Current simulated time in seconds.
                now_epoch = int(demo.clock.now().timestamp())
                # No visible hold by default.
                held = None
                # Only an unexpired hold is shown, with the seconds left.
                if hold and hold["expires_epoch"] > now_epoch:
                    held = {"slot": f"{_hhmm(hold['slot_start_ts'])}-{_hhmm(hold['slot_end_ts'])} at dock {hold['dock_code']}",
                            "seconds_left": hold["expires_epoch"] - now_epoch}
                # The driver's hand-off case, in plain words.
                case = get_driver_case_status(conn, driver_id)
                return {"messages": msgs, "hold": held, "case": {"status": case.status, "text": case.text} if case else None, "clock": clock_info()}
            finally:
                conn.close()

        return local_only(request) or await run_in_threadpool(read)

    # Build the GPS readout for one load: the dock, the latest ping, distance, travel time and GPS ETA. Used by both GPS routes.
    def gps_readout(conn: sqlite3.Connection, shipment_id: str) -> dict[str, Any]:
        settings = load_settings()
        now = demo.clock.now()
        # The destination facility (the dock the truck is heading for).
        fac = conn.execute("select f.facility_name, f.lat, f.lon from shipments s join facilities f on f.facility_id=s.destination_facility_id "
                           "where s.shipment_id=?", (shipment_id,)).fetchone()
        out: dict[str, Any] = {"facility": {"lat": fac["lat"], "lon": fac["lon"], "name": fac["facility_name"]},
                               "ping": None, "distance_km": None, "travel_minutes": None, "gps_eta": None,
                               # False when the dock has no coordinates (a facility without them simply gets no GPS ETA).
                               "has_coordinates": fac["lat"] is not None and fac["lon"] is not None}
        # The latest ping by recorded time, whatever its age (the readout shows stale ones too).
        row = conn.execute("select lat, lon, recorded_at from gps_pings where shipment_id=? order by recorded_at desc, ping_id desc limit 1",
                           (shipment_id,)).fetchone()
        if row is None:
            return out
        # A future-dated ping counts as age zero.
        age = max(now - parse_operational_timestamp(row["recorded_at"]), timedelta(0))
        fresh = age <= timedelta(minutes=settings.gps_max_age_minutes)
        out["ping"] = {"lat": row["lat"], "lon": row["lon"], "age_minutes": int(age.total_seconds() // 60), "fresh": fresh}
        # No dock coordinates: show the ping but no distance, travel time or GPS ETA.
        if not out["has_coordinates"]:
            return out
        out["distance_km"] = round(haversine_km(row["lat"], row["lon"], fac["lat"], fac["lon"]), 1)
        # Travel time and ETA only come from a fresh ping (the same rule the ETA tools use).
        travel = gps_travel_minutes_for(conn, shipment_id, demo.clock, settings)
        if travel is not None:
            out["travel_minutes"] = travel
            out["gps_eta"] = f"{(now + timedelta(minutes=travel)).astimezone(IST):%H:%M}"
        return out

    # Send a GPS position as the driver's phone would (without the signature step) and read back what the service makes of it.
    @app.post("/demo/api/gps")
    async def gps_send(request: Request, body: GpsIn) -> Any:
        denied = local_only(request)
        if denied:
            return denied

        def go() -> Any:
            conn = db()
            try:
                # Unknown driver.
                if conn.execute("select 1 from drivers where driver_id=?", (body.driver_id,)).fetchone() is None:
                    return JSONResponse({"status": "REJECTED", "reason_code": "UNKNOWN_DRIVER"}, status_code=404)
                # The load being driven.
                sid = active_shipment_for_driver(conn, body.driver_id)
                if sid is None:
                    return JSONResponse({"status": "REJECTED", "reason_code": "NO_ACTIVE_SHIPMENT"}, status_code=404)
                # Stamp the fix as old as the console asked; the demo clock itself does not move.
                recorded = demo.clock.now() - timedelta(minutes=body.age_minutes)
                with immediate(conn):
                    # In the demo the ping just sent is the truck's latest word, so drop any ping stamped later (for example the seeded
                    # one); otherwise a deliberately old test ping would be ignored in favour of it. The real API never does this.
                    # Both timestamps are normalised to IST by store_ping, so this lexical comparison is chronological.
                    # Across all the driver's loads: the position lookup is per driver, so an older ping on any load must not shadow this one.
                    for shipment_id in open_shipments_for_driver(conn, body.driver_id):
                        conn.execute("delete from gps_pings where shipment_id=? and recorded_at>?", (shipment_id, recorded.astimezone(IST).isoformat()))
                    store_ping(conn, sid, GpsPing(lat=body.lat, lon=body.lon, recorded_at=recorded), demo.clock, "DEMO_CONSOLE")
                # The truck has a new position: confirm it to the driver, release a hold that is now out of reach, and offer earlier slots
                # if the truck is much closer (exactly as the real API does).
                memory = ConversationMemoryStore(conn, deps.redis, demo.clock, load_settings(), hold_checker=make_hold_checker(conn, demo.clock))
                handle_driver_position(conn, body.driver_id, demo.clock, memory)
                return gps_readout(conn, sid)
            finally:
                conn.close()

        return await run_in_threadpool(go)

    # The GPS readout for a driver's active load.
    @app.get("/demo/api/driver/{driver_id}/gps")
    async def driver_gps(request: Request, driver_id: str) -> Any:
        def read() -> Any:
            conn = db(True)
            try:
                if conn.execute("select 1 from drivers where driver_id=?", (driver_id,)).fetchone() is None:
                    return JSONResponse({"status": "REJECTED", "reason_code": "UNKNOWN_DRIVER"}, status_code=404)
                sid = active_shipment_for_driver(conn, driver_id)
                if sid is None:
                    return JSONResponse({"status": "REJECTED", "reason_code": "NO_ACTIVE_SHIPMENT"}, status_code=404)
                return gps_readout(conn, sid)
            finally:
                conn.close()

        return local_only(request) or await run_in_threadpool(read)

    # The dock-by-hour board for one facility.
    @app.get("/demo/api/board")
    async def board(request: Request, facility_id: str = "FAC-JAI-01") -> Any:
        def read() -> Any:
            conn = db(True)
            try:
                # Unknown facility.
                if conn.execute("select 1 from facilities where facility_id=?", (facility_id,)).fetchone() is None:
                    return JSONResponse({"status": "REJECTED", "reason_code": "UNKNOWN_FACILITY"}, status_code=404)
                # The same snapshot the service uses to judge slots.
                state = build_facility_state(conn, facility_id, demo.clock.now())
                # Who occupies each slot, as 'driver . order'.
                owner = {r["slot_id"]: (r["driver_id"], f"{r['driver_name']} · {r['order_reference']}") for r in conn.execute(
                    "select a.slot_id, d.driver_id, d.driver_name, s.order_reference from appointments a join shipments s using(shipment_id) "
                    "join drivers d on d.driver_id=s.driver_id where a.appointment_status in ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS','HELD')")}
                now = demo.clock.now()
                # One entry per dock.
                docks: list[dict[str, Any]] = []
                # Docks in code order (D1, D2 ...).
                for dock in sorted(state.docks.values(), key=lambda d: d.code):
                    # Slots of this dock.
                    slots: list[dict[str, Any]] = []
                    # Slots in time order.
                    for slot in sorted((s for s in state.slots.values() if s.dock_id == dock.dock_id), key=lambda s: s.start):
                        # Who occupies it, if anyone (unexpired holds count).
                        occ = state.occupancy.get(slot.slot_id)
                        # Blocked by a planner, an out-of-service dock or a maintenance event.
                        blocked = slot.status != "OPEN" or dock.status != "ACTIVE" or any(
                            e.start < slot.end and (e.end is None or e.end > slot.start) for e in state.effective_events(dock.dock_id))
                        # BLOCKED, HELD, BOOKED or AVAILABLE.
                        status = "BLOCKED" if blocked else ("HELD" if occ and occ.status == "HELD" else "BOOKED" if occ else "AVAILABLE")
                        # The occupant's label.
                        who = owner.get(slot.slot_id) if occ else None
                        # One cell of the board.
                        slots.append({"slot_id": slot.slot_id, "hour": slot.start.astimezone(IST).hour, "start": f"{slot.start.astimezone(IST):%H:%M}",
                                      "end": f"{slot.end.astimezone(IST):%H:%M}", "status": status, "past": slot.start < now,
                                      "label": who[1] if who else (slot.block_reason or ""), "driver_id": who[0] if who else None})
                    # One row of the board.
                    docks.append({"code": dock.code, "type": dock.dock_type.title(), "max_kg": dock.max_weight_kg, "slots": slots})
                # The hours that have slots, for the column headings.
                hours = sorted({s["hour"] for d in docks for s in d["slots"]})
                return {"facility": facility_id, "hours": hours, "docks": docks, "clock": clock_info()}
            finally:
                conn.close()

        return local_only(request) or await run_in_threadpool(read)

    # Book a slot as the warehouse planner, to create a conflict with a driver's hold.
    @app.post("/demo/api/planner-booking")
    async def planner_booking(request: Request, body: SlotIn) -> Any:
        denied = local_only(request)
        if denied:
            return denied

        def go() -> Any:
            conn = db()
            try:
                # Time stamp for the booking.
                now = demo.clock.now().isoformat()
                # One write transaction.
                with immediate(conn):
                    # Pick a load that has no live appointment, so no real driver is disturbed.
                    spare = next((s for s in SPARE_SHIPMENTS if conn.execute(
                        "select 1 from appointments where shipment_id=? and is_current=1 and appointment_status in "
                        "('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')", (s,)).fetchone() is None), None)
                    # No spare load left: 409.
                    if spare is None:
                        return JSONResponse({"status": "REJECTED", "reason_code": "NO_SPARE_LOAD"}, status_code=409)
                    try:
                        # Insert a CONFIRMED planner booking. The shared unique index rejects it if the slot is taken or held.
                        conn.execute("insert into appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source, is_current, "
                                     "booked_at, updated_at) values (?,?,?, 'CONFIRMED','PLANNER',1,?,?)",
                                     (f"PLAN-{uuid.uuid4().hex[:8].upper()}", spare, body.slot_id, now, now))
                    # The database refused: the slot is not free (409 SLOT_NOT_FREE).
                    except sqlite3.IntegrityError:
                        return JSONResponse({"status": "REJECTED", "reason_code": "SLOT_NOT_FREE"}, status_code=409)
                # Say whose load was booked.
                return {"status": "BOOKED", "for": names(conn).get(spare, "another truck")}
            finally:
                conn.close()

        return await run_in_threadpool(go)

    # Move simulated time forward, optionally running the hold-expiry sweep.
    @app.post("/demo/api/clock")
    async def clock(request: Request, body: ClockIn) -> Any:
        denied = local_only(request)
        if denied:
            return denied

        def go() -> dict[str, Any]:
            # Jump ahead by the requested seconds.
            demo.clock.advance(timedelta(seconds=body.seconds))
            # Number of holds expired by the sweep.
            swept = 0
            # Run the background sweep now, as the real job would.
            if body.sweep:
                conn = db()
                try:
                    # Expire holds whose time has passed.
                    swept = expire_due_holds(conn, demo.clock).expired
                finally:
                    conn.close()
            return {"clock": clock_info(), "swept": swept}

        return await run_in_threadpool(go)

    # Reset: new database, empty cache, clock back to the start.
    @app.post("/demo/api/reset")
    async def reset(request: Request, body: ResetIn | None = None) -> Any:
        denied = local_only(request)
        if denied:
            return denied
        # Run the blocking reset in a worker thread, optionally switching between the supplied schedule and an empty board.
        await run_in_threadpool(demo.reset, body.fresh if body else None)
        return {"status": "RESET", "fresh": demo.fresh, "clock": clock_info()}

    # The hand-off queue for the operations panel.
    @app.get("/demo/api/cases")
    async def cases(request: Request) -> Any:
        def read() -> dict[str, Any]:
            conn = db(True)
            try:
                # Driver names by id.
                who = {r["driver_id"]: r["driver_name"] for r in conn.execute("select driver_id, driver_name from drivers")}
                # Order and customer by shipment.
                order = {r["shipment_id"]: f"{r['order_reference']} · {r['customer_name']}" for r in conn.execute("select * from shipments")}
                # All cases, any status, up to 40.
                rows = list_open_cases(conn, ("OPEN", "CLAIMED", "RESOLVED", "CANCELLED"), 40)
                out = []
                # Newest first.
                for c in reversed(rows):
                    ctx = c.context
                    # A case card with plain-language reason, driver, load and last messages. No raw ids are the headline.
                    out.append({"case_id": c.case_id, "status": c.status, "reason": REASON_TEXT.get(c.reason_code, c.reason_code), "reason_code": c.reason_code,
                                "driver": who.get(c.driver_id, "Unknown driver"), "load": order.get(c.shipment_id or "", "no load named"), "summary": c.summary,
                                "opened": _hhmmss(c.created_at), "claimed_by": c.claimed_by, "resolution": c.resolution_note,
                                "missing": ctx.get("missing", []), "eta": ctx.get("eta"),
                                "recent": [f"{t['role']}: {t['text'][:140]}" for t in ctx.get("recent_turns", [])][-3:]})
                return {"cases": out, "clock": clock_info()}
            finally:
                conn.close()

        return local_only(request) or await run_in_threadpool(read)

    # A coordinator claims a case.
    @app.post("/demo/api/cases/{case_id}/claim")
    async def claim(request: Request, case_id: str, body: ClaimIn) -> Any:
        denied = local_only(request)
        if denied:
            return denied

        def go() -> Any:
            conn = db()
            try:
                # The real queue operation.
                r = claim_case(conn, case_id, body.coordinator, demo.clock)
                return JSONResponse({"status": r.status, "claimed_by": r.claimed_by}, status_code=200 if r.status in ("CLAIMED",) or r.replayed else 409)
            finally:
                conn.close()

        return await run_in_threadpool(go)

    # A coordinator resolves a case with a code and note.
    @app.post("/demo/api/cases/{case_id}/resolve")
    async def resolve(request: Request, case_id: str, body: ResolveIn) -> Any:
        denied = local_only(request)
        if denied:
            return denied

        def go() -> Any:
            conn = db()
            try:
                # The real queue operation.
                r = resolve_case(conn, case_id, body.coordinator, body.resolution_code, body.note, demo.clock)
                return JSONResponse({"status": r.status, "claimed_by": r.claimed_by}, status_code=200 if r.status == "RESOLVED" else 409)
            finally:
                conn.close()

        return await run_in_threadpool(go)

    # Appointment history and the notification outbox.
    @app.get("/demo/api/activity")
    async def activity(request: Request) -> Any:
        def read() -> dict[str, Any]:
            conn = db(True)
            try:
                # Shipment -> readable label.
                label = names(conn)
                # Slot -> 'HH:MM at dock Dn'.
                slot_text = {r["slot_id"]: f"{_hhmm(r['slot_start_ts'])} at dock {r['dock_code']}" for r in conn.execute(
                    "select s.slot_id, s.slot_start_ts, d.dock_code from appointment_slots s join docks d using(dock_id)")}
                # Recent history events.
                events = []
                # The 30 most recent events, newest first.
                for e in conn.execute("select e.*, a.slot_id from appointment_events e join appointments a using(appointment_id) order by e.event_id desc limit 30"):
                    d = json.loads(e["details_json"])
                    slot = slot_text.get(e["slot_id"], "")
                    # A readable sentence per event type.
                    text = {"HOLD_ACQUIRED": f"held the {slot} slot", "HOLD_RELEASED": f"released the hold on {slot}", "HOLD_EXPIRED": f"hold on {slot} lapsed",
                            "ORIGINAL_ENDED": f"original appointment ({slot}) ended because a new one was confirmed",
                            "RESCHEDULE_CONFIRMED": f"appointment confirmed for {slot}", "APPOINTMENT_CANCELLED": f"appointment at {slot} cancelled"}.get(e["event_type"], e["event_type"])
                    # One history line.
                    events.append({"time": _hhmmss(parse_operational_timestamp(e["occurred_at"])), "who": label.get(e["shipment_id"], ""), "text": text,
                                   "by": {"DRIVER": "driver", "SYSTEM": "system"}.get(e["actor_type"], e["actor_type"].lower()), "extra": d.get("rank")})
                # Recent notification outbox rows.
                outbox = []
                # The 20 most recent notices.
                for o in conn.execute("select recipient_type, recipient_ref, status, attempts, payload_json from notification_outbox order by outbox_id desc limit 20"):
                    # One notice line.
                    outbox.append({"to": o["recipient_ref"], "role": o["recipient_type"].replace("_", " ").title(), "status": o["status"], "attempts": o["attempts"],
                                   "kind": json.loads(o["payload_json"]).get("kind", "").replace("_", " ").title()})
                # The 10 most recent GPS pings, newest first, each explained with the arithmetic behind the GPS ETA.
                settings = load_settings()
                estimator = StraightLineEstimator(settings)
                gps = []
                for p in conn.execute("select p.shipment_id, p.lat, p.lon, p.recorded_at, p.received_at, f.lat as flat, f.lon as flon from gps_pings p "
                                      "join shipments s using(shipment_id) join facilities f on f.facility_id=s.destination_facility_id "
                                      "order by p.ping_id desc limit 10"):
                    received = parse_operational_timestamp(p["received_at"])
                    # Ping age when it arrived.
                    age = max(received - parse_operational_timestamp(p["recorded_at"]), timedelta(0))
                    # A dock without coordinates cannot be measured from: say so instead of computing a distance.
                    if p["flat"] is None or p["flon"] is None:
                        gps.append({"time": _hhmmss(received), "text": f"{label.get(p['shipment_id'], '')}: dock has no coordinates -> no GPS ETA"})
                        continue
                    km = haversine_km(p["lat"], p["lon"], p["flat"], p["flon"])
                    head = f"{label.get(p['shipment_id'], '')}: ping {int(age.total_seconds() // 60)} min old, {km:.1f} km from dock"
                    # A stale ping gives no estimate.
                    if age > timedelta(minutes=settings.gps_max_age_minutes):
                        text = f"{head}, too old to use (limit {settings.gps_max_age_minutes} min) -> no GPS ETA"
                    else:
                        mins = estimator.estimate(p["lat"], p["lon"], p["flat"], p["flon"])
                        text = (f"{head}, x{settings.gps_road_factor:g} road factor at {settings.gps_avg_speed_kmph:g} km/h = {mins} min"
                                f" -> GPS ETA {(received + timedelta(minutes=mins)).astimezone(IST):%H:%M}")
                    gps.append({"time": _hhmmss(received), "text": text})
                return {"events": events, "outbox": outbox, "gps": gps, "clock": clock_info()}
            finally:
                conn.close()

        return local_only(request) or await run_in_threadpool(read)

    # Deliver queued downstream notices now.
    @app.post("/demo/api/deliver")
    async def deliver(request: Request) -> Any:
        denied = local_only(request)
        if denied:
            return denied

        def go() -> dict[str, Any]:
            conn = db()
            try:
                # A stand-in receiver that accepts everything, to show the outbox draining.
                r = deliver_pending_notifications(conn, demo.clock, lambda n: None)  # a stand-in receiver that accepts everything
                return {"sent": r.sent, "failed": r.failed}
            finally:
                conn.close()

        return await run_in_threadpool(go)


def build_demo_app(workdir: Path | None = None, live_factory: Callable[[], Any] | None = None, policy: Any = None,
                   fresh: bool = False) -> tuple[FastAPI, Any]:
    """The service wired for the demo: fixed clock, in-process cache, the configured model (chat is off without one), no real channel."""
    # An in-process fake Redis, so no server is needed.
    import fakeredis

    # The real app factory.
    from setuhaul.api import AppDeps, UnconfiguredGateway, create_app

    # Model limits.
    from setuhaul.model_gateway import ModelPolicy

    # A working folder: the one given, or a fresh temporary one.
    work = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="setuhaul-demo-"))
    work.mkdir(parents=True, exist_ok=True)
    # The demo database file.
    db_path = work / "demo.sqlite3"
    # A simulated clock starting at the demo moment.
    clock = FakeClock(parse_operational_timestamp(DEMO_START))
    # The fake cache.
    redis = fakeredis.FakeRedis(decode_responses=True)

    # Build the first database.
    build_demo_db(db_path, fresh)
    # Shared state for the routes (its reset function is attached just below).
    state = DemoState(clock=clock, db_path=db_path, reset=lambda **_: None, fresh=fresh, live_factory=live_factory,
                      model_name=getattr(policy, "model_id", "") if live_factory else "")
    # Counts resets, so each one gets its own database file.
    generation = {"n": 0}

    # Rebuild everything to the starting state; pass fresh=True/False to switch the board mode, or nothing to keep it.
    def reset(fresh: bool | None = None) -> None:
        if fresh is not None:
            state.fresh = fresh
        # A NEW database file each time: deleting the old one fails on Windows while the page's polling still has it open.
        generation["n"] += 1
        old = state.db_path
        new = work / f"demo-{generation['n']}.sqlite3"
        build_demo_db(new, state.fresh)
        # Switch every route (and the message pipeline) to the new file.
        state.db_path = new
        deps.db_path = new
        # Empty the cache.
        redis.flushall()
        # Clock back to the start.
        clock.set(parse_operational_timestamp(DEMO_START))
        # Remove the old file and its WAL files if possible; leftovers are harmless.
        for suffix in ("", "-wal", "-shm"):
            _try_unlink(Path(str(old) + suffix))

    state.reset = reset
    # Wire the real app: demo clock, fake cache, a throwaway webhook secret, and the configured model (never a stand-in).
    deps = AppDeps(
        db_path=db_path, redis=redis, webhook_secret=os.urandom(16), coordinator_tokens={}, fallback_path=work / "fallback.jsonl",
        policy=policy or ModelPolicy(provider="none", model_id=""), clock=clock, demo=state,
        gateway_factory=live_factory if live_factory else UnconfiguredGateway)
    # Remove a stray folder if one exists (harmless if not).
    shutil.rmtree(work / "unused", ignore_errors=True)
    # Return the app and its dependencies.
    return create_app(deps), deps
