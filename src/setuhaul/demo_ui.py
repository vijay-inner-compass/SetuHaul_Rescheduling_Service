"""Demo screen: a localhost-only web page that drives the real service end to end.

NOT part of the production surface. It exists so people can watch the whole flow: a driver chatting, the dock board
changing as holds and bookings happen, and a coordinator working the escalation queue. Every driver message goes through
the same code path as the webhook (rate limit, duplicate handling, conversation, tools); only the signature step is skipped,
because the server stands in for the messaging channel. It runs on a fixed clock (the snapshot is dated 2026-08-04) with
buttons to move time forward, and refuses any request that does not come from the local machine.

The page shows people, not ids: drivers by name, loads by order reference and customer.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from setuhaul.capacity import build_facility_state
from setuhaul.clock import FakeClock
from setuhaul.db import (
    IST,
    immediate,
    make_working_db,
    open_connection,
    parse_operational_timestamp,
)
from setuhaul.escalation import claim_case, get_driver_case_status, list_open_cases, resolve_case
from setuhaul.identity import provision_identities
from setuhaul.jobs.expire_holds import expire_due_holds
from setuhaul.notifications import deliver_pending_notifications

DEMO_START = "2026-08-04T09:35:00+05:30"
MESSAGE_ADVANCE_SECONDS = 20  # each driver message takes a moment, so holds can lapse during a demo
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
SPARE_SHIPMENTS = ("SHP1018", "SHP1019", "SHP1008", "SHP1001")  # loads the planner can book without touching a driver in the demo
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
class DemoState:
    clock: FakeClock
    db_path: Path
    reset: Callable[[], None]
    live_factory: Callable[[], Any] | None = None  # a real model gateway, if one is configured
    model_mode: str = "stub"
    coordinators: tuple[str, ...] = ("Asha Verma", "Ben Thomas")
    notes: dict[str, Any] = field(default_factory=dict)


class ChatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    driver_id: str
    text: str = Field(min_length=1, max_length=2000)


class ClockIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seconds: int = Field(ge=1, le=7200)
    sweep: bool = False


class SlotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot_id: str


class ModelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str


class ClaimIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coordinator: str


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coordinator: str
    resolution_code: str
    note: str = Field(max_length=500)


def seed_demo_evidence(conn: sqlite3.Connection) -> None:
    """Labour and consignee evidence the snapshot lacks, labelled DEMO_FIXTURE, plus a clean chat history."""
    day = "2026-08-04"
    for fac in ("FAC-JAI-01", "FAC-GGN-01"):
        conn.execute("insert into labour_windows (facility_id, window_start, window_end, available_crews, equipment_available, source, recorded_at) "
                     "values (?,?,?,8,1,'DEMO_FIXTURE',?)", (fac, f"{day}T06:00:00+05:30", f"{day}T22:00:00+05:30", f"{day}T06:00:00+05:30"))
    for (sid,) in conn.execute("select shipment_id from shipments").fetchall():
        conn.execute("insert or replace into consignee_commitments (shipment_id, latest_arrival, penalty_applies, exceptional, source, recorded_at) "
                     "values (?, NULL, 0, 0, 'DEMO_FIXTURE', ?)", (sid, f"{day}T06:00:00+05:30"))
    conn.execute("delete from chat_messages")  # the demo starts every conversation from nothing
    conn.commit()
    provision_identities(conn)


def build_demo_db(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    make_working_db(path)
    conn = open_connection(path=path)
    try:
        seed_demo_evidence(conn)
    finally:
        conn.close()


def _hhmm(raw: str) -> str:
    return f"{parse_operational_timestamp(raw).astimezone(IST):%H:%M}"


def _hhmmss(dt: datetime) -> str:
    return f"{dt.astimezone(IST):%H:%M:%S}"


def register_demo(app: FastAPI, deps: Any, process: Callable[[Any, str], JSONResponse]) -> None:
    demo: DemoState = deps.demo
    html = (Path(__file__).parent / "demo" / "index.html").read_text(encoding="utf-8")

    def local_only(request: Request) -> JSONResponse | None:
        host = request.client.host if request.client else ""
        if host not in LOCAL_HOSTS:
            return JSONResponse({"status": "REJECTED", "reason_code": "DEMO_IS_LOCAL_ONLY"}, status_code=403)
        return None

    def db(readonly: bool = False) -> sqlite3.Connection:
        return open_connection(path=demo.db_path, readonly=readonly)

    def names(conn: sqlite3.Connection) -> dict[str, str]:
        return {r["shipment_id"]: f"{r['driver_name']} · {r['order_reference']}" for r in conn.execute(
            "select s.shipment_id, s.order_reference, d.driver_name from shipments s join drivers d using(driver_id)")}

    def clock_info() -> dict[str, Any]:
        now = demo.clock.now()
        return {"iso": now.isoformat(), "text": f"{now.astimezone(IST):%a %d %b %Y, %H:%M:%S} IST"}

    @app.get("/demo", response_class=HTMLResponse)
    async def page(request: Request) -> Any:
        return local_only(request) or HTMLResponse(html)

    @app.get("/demo/api/config")
    async def config(request: Request) -> Any:
        def read() -> dict[str, Any]:
            conn = db(True)
            try:
                facilities = [{"id": r["facility_id"], "name": r["facility_name"]} for r in conn.execute("select * from facilities order by facility_name")]
            finally:
                conn.close()
            return {"clock": clock_info(), "model_mode": demo.model_mode, "live_available": demo.live_factory is not None,
                    "coordinators": list(demo.coordinators), "facilities": facilities, "message_advance_seconds": MESSAGE_ADVANCE_SECONDS}

        return local_only(request) or await run_in_threadpool(read)

    @app.get("/demo/api/drivers")
    async def drivers(request: Request) -> Any:
        def read() -> dict[str, Any]:
            conn = db(True)
            try:
                out = []
                for d in conn.execute("select d.driver_id, d.driver_name, d.home_base_city, ca.carrier_name from drivers d "
                                      "join carriers ca using(carrier_id) where d.driver_status='ACTIVE' and d.driver_id not like 'GEN-%' order by d.driver_name"):
                    ships = []
                    for s in conn.execute(
                            "select s.shipment_id, s.order_reference, s.customer_name, s.current_status, f.facility_name, v.registration_number, "
                            "le.effective_eta_ts, sl.slot_start_ts, dk.dock_code from shipments s join facilities f on f.facility_id=s.destination_facility_id "
                            "join vehicles v using(vehicle_id) join v_latest_eta le using(shipment_id) left join appointments a on a.shipment_id=s.shipment_id "
                            "and a.is_current=1 and a.appointment_status in ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS') "
                            "left join appointment_slots sl on sl.slot_id=a.slot_id left join docks dk on dk.dock_id=sl.dock_id "
                            "where s.driver_id=? and s.current_status not in ('COMPLETED','CANCELLED') order by s.shipment_id", (d["driver_id"],)):
                        ships.append({"order": s["order_reference"], "customer": s["customer_name"], "status": s["current_status"].replace("_", " ").title(),
                                      "destination": s["facility_name"], "truck": s["registration_number"], "eta": _hhmm(s["effective_eta_ts"]),
                                      "appointment": f"{_hhmm(s['slot_start_ts'])} at dock {s['dock_code']}" if s["slot_start_ts"] else None})
                    out.append({"id": d["driver_id"], "name": d["driver_name"], "city": d["home_base_city"], "carrier": d["carrier_name"], "shipments": ships})
                return {"drivers": out}
            finally:
                conn.close()

        return local_only(request) or await run_in_threadpool(read)

    @app.post("/demo/api/chat")
    async def chat(request: Request, body: ChatIn) -> Any:
        denied = local_only(request)
        if denied:
            return denied

        def go() -> dict[str, Any]:
            from setuhaul.api import MessageIn

            conn = db(True)
            try:
                row = conn.execute("select phone from drivers where driver_id=?", (body.driver_id,)).fetchone()
            finally:
                conn.close()
            if row is None:
                return {"http_status": 404, "status": "REJECTED", "reason_code": "UNKNOWN_DRIVER"}
            payload = MessageIn.model_validate({"from": row["phone"], "message_id": f"DEMO-{uuid.uuid4().hex[:12]}", "text": body.text})
            resp = process(payload, request.state.request_id)
            data = json.loads(bytes(resp.body))
            demo.clock.advance(timedelta(seconds=MESSAGE_ADVANCE_SECONDS))
            return {"http_status": resp.status_code, **data, "clock": clock_info()}

        return await run_in_threadpool(go)

    @app.get("/demo/api/driver/{driver_id}/state")
    async def driver_state(request: Request, driver_id: str) -> Any:
        def read() -> dict[str, Any]:
            conn = db(True)
            try:
                thread = conn.execute("select thread_id from chat_threads where driver_id=? and thread_status<>'CLOSED' order by opened_at desc, rowid desc limit 1",
                                      (driver_id,)).fetchone()
                msgs = []
                if thread:
                    for m in conn.execute("select sender_type, message_text, message_ts from chat_messages where thread_id=? and is_duplicate=0 "
                                          "order by rowid desc limit 40", (thread["thread_id"],)):
                        who = {"DRIVER": "driver", "AGENT": "assistant"}.get(m["sender_type"], "operations")
                        msgs.append({"who": who, "text": m["message_text"], "time": _hhmmss(parse_operational_timestamp(m["message_ts"]))})
                    msgs.reverse()
                hold = conn.execute(
                    "select a.appointment_id, sl.slot_start_ts, sl.slot_end_ts, dk.dock_code, m.expires_epoch from appointments a "
                    "join appointment_hold_meta m using(appointment_id) join appointment_slots sl using(slot_id) join docks dk on dk.dock_id=sl.dock_id "
                    "where m.driver_id=? and a.appointment_status='HELD'", (driver_id,)).fetchone()
                now_epoch = int(demo.clock.now().timestamp())
                held = None
                if hold and hold["expires_epoch"] > now_epoch:
                    held = {"slot": f"{_hhmm(hold['slot_start_ts'])}-{_hhmm(hold['slot_end_ts'])} at dock {hold['dock_code']}",
                            "seconds_left": hold["expires_epoch"] - now_epoch}
                case = get_driver_case_status(conn, driver_id)
                return {"messages": msgs, "hold": held, "case": {"status": case.status, "text": case.text} if case else None, "clock": clock_info()}
            finally:
                conn.close()

        return local_only(request) or await run_in_threadpool(read)

    @app.get("/demo/api/board")
    async def board(request: Request, facility_id: str = "FAC-JAI-01") -> Any:
        def read() -> Any:
            conn = db(True)
            try:
                if conn.execute("select 1 from facilities where facility_id=?", (facility_id,)).fetchone() is None:
                    return JSONResponse({"status": "REJECTED", "reason_code": "UNKNOWN_FACILITY"}, status_code=404)
                state = build_facility_state(conn, facility_id, demo.clock.now())
                owner = {r["slot_id"]: (r["driver_id"], f"{r['driver_name']} · {r['order_reference']}") for r in conn.execute(
                    "select a.slot_id, d.driver_id, d.driver_name, s.order_reference from appointments a join shipments s using(shipment_id) "
                    "join drivers d on d.driver_id=s.driver_id where a.appointment_status in ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS','HELD')")}
                now = demo.clock.now()
                docks: list[dict[str, Any]] = []
                for dock in sorted(state.docks.values(), key=lambda d: d.code):
                    slots: list[dict[str, Any]] = []
                    for slot in sorted((s for s in state.slots.values() if s.dock_id == dock.dock_id), key=lambda s: s.start):
                        occ = state.occupancy.get(slot.slot_id)
                        blocked = slot.status != "OPEN" or dock.status != "ACTIVE" or any(
                            e.start < slot.end and (e.end is None or e.end > slot.start) for e in state.effective_events(dock.dock_id))
                        status = "BLOCKED" if blocked else ("HELD" if occ and occ.status == "HELD" else "BOOKED" if occ else "AVAILABLE")
                        who = owner.get(slot.slot_id) if occ else None
                        slots.append({"slot_id": slot.slot_id, "hour": slot.start.astimezone(IST).hour, "start": f"{slot.start.astimezone(IST):%H:%M}",
                                      "end": f"{slot.end.astimezone(IST):%H:%M}", "status": status, "past": slot.start < now,
                                      "label": who[1] if who else (slot.block_reason or ""), "driver_id": who[0] if who else None})
                    docks.append({"code": dock.code, "type": dock.dock_type.title(), "max_kg": dock.max_weight_kg, "slots": slots})
                hours = sorted({s["hour"] for d in docks for s in d["slots"]})
                return {"facility": facility_id, "hours": hours, "docks": docks, "clock": clock_info()}
            finally:
                conn.close()

        return local_only(request) or await run_in_threadpool(read)

    @app.post("/demo/api/planner-booking")
    async def planner_booking(request: Request, body: SlotIn) -> Any:
        denied = local_only(request)
        if denied:
            return denied

        def go() -> Any:
            conn = db()
            try:
                now = demo.clock.now().isoformat()
                with immediate(conn):
                    spare = next((s for s in SPARE_SHIPMENTS if conn.execute(
                        "select 1 from appointments where shipment_id=? and is_current=1 and appointment_status in "
                        "('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')", (s,)).fetchone() is None), None)
                    if spare is None:
                        return JSONResponse({"status": "REJECTED", "reason_code": "NO_SPARE_LOAD"}, status_code=409)
                    try:
                        conn.execute("insert into appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source, is_current, "
                                     "booked_at, updated_at) values (?,?,?, 'CONFIRMED','PLANNER',1,?,?)",
                                     (f"PLAN-{uuid.uuid4().hex[:8].upper()}", spare, body.slot_id, now, now))
                    except sqlite3.IntegrityError:
                        return JSONResponse({"status": "REJECTED", "reason_code": "SLOT_NOT_FREE"}, status_code=409)
                return {"status": "BOOKED", "for": names(conn).get(spare, "another truck")}
            finally:
                conn.close()

        return await run_in_threadpool(go)

    @app.post("/demo/api/clock")
    async def clock(request: Request, body: ClockIn) -> Any:
        denied = local_only(request)
        if denied:
            return denied

        def go() -> dict[str, Any]:
            demo.clock.advance(timedelta(seconds=body.seconds))
            swept = 0
            if body.sweep:
                conn = db()
                try:
                    swept = expire_due_holds(conn, demo.clock).expired
                finally:
                    conn.close()
            return {"clock": clock_info(), "swept": swept}

        return await run_in_threadpool(go)

    @app.post("/demo/api/model")
    async def model(request: Request, body: ModelIn) -> Any:
        denied = local_only(request)
        if denied:
            return denied
        if body.mode not in ("stub", "live") or (body.mode == "live" and demo.live_factory is None):
            return JSONResponse({"status": "REJECTED", "reason_code": "MODEL_NOT_AVAILABLE"}, status_code=409)
        demo.model_mode = body.mode
        return {"model_mode": demo.model_mode}

    @app.post("/demo/api/reset")
    async def reset(request: Request) -> Any:
        denied = local_only(request)
        if denied:
            return denied
        await run_in_threadpool(demo.reset)
        return {"status": "RESET", "clock": clock_info()}

    @app.get("/demo/api/cases")
    async def cases(request: Request) -> Any:
        def read() -> dict[str, Any]:
            conn = db(True)
            try:
                who = {r["driver_id"]: r["driver_name"] for r in conn.execute("select driver_id, driver_name from drivers")}
                order = {r["shipment_id"]: f"{r['order_reference']} · {r['customer_name']}" for r in conn.execute("select * from shipments")}
                rows = list_open_cases(conn, ("OPEN", "CLAIMED", "RESOLVED", "CANCELLED"), 40)
                out = []
                for c in reversed(rows):
                    ctx = c.context
                    out.append({"case_id": c.case_id, "status": c.status, "reason": REASON_TEXT.get(c.reason_code, c.reason_code), "reason_code": c.reason_code,
                                "driver": who.get(c.driver_id, "Unknown driver"), "load": order.get(c.shipment_id or "", "no load named"), "summary": c.summary,
                                "opened": _hhmmss(c.created_at), "claimed_by": c.claimed_by, "resolution": c.resolution_note,
                                "missing": ctx.get("missing", []), "eta": ctx.get("eta"),
                                "recent": [f"{t['role']}: {t['text'][:140]}" for t in ctx.get("recent_turns", [])][-3:]})
                return {"cases": out, "clock": clock_info()}
            finally:
                conn.close()

        return local_only(request) or await run_in_threadpool(read)

    @app.post("/demo/api/cases/{case_id}/claim")
    async def claim(request: Request, case_id: str, body: ClaimIn) -> Any:
        denied = local_only(request)
        if denied:
            return denied

        def go() -> Any:
            conn = db()
            try:
                r = claim_case(conn, case_id, body.coordinator, demo.clock)
                return JSONResponse({"status": r.status, "claimed_by": r.claimed_by}, status_code=200 if r.status in ("CLAIMED",) or r.replayed else 409)
            finally:
                conn.close()

        return await run_in_threadpool(go)

    @app.post("/demo/api/cases/{case_id}/resolve")
    async def resolve(request: Request, case_id: str, body: ResolveIn) -> Any:
        denied = local_only(request)
        if denied:
            return denied

        def go() -> Any:
            conn = db()
            try:
                r = resolve_case(conn, case_id, body.coordinator, body.resolution_code, body.note, demo.clock)
                return JSONResponse({"status": r.status, "claimed_by": r.claimed_by}, status_code=200 if r.status == "RESOLVED" else 409)
            finally:
                conn.close()

        return await run_in_threadpool(go)

    @app.get("/demo/api/activity")
    async def activity(request: Request) -> Any:
        def read() -> dict[str, Any]:
            conn = db(True)
            try:
                label = names(conn)
                slot_text = {r["slot_id"]: f"{_hhmm(r['slot_start_ts'])} at dock {r['dock_code']}" for r in conn.execute(
                    "select s.slot_id, s.slot_start_ts, d.dock_code from appointment_slots s join docks d using(dock_id)")}
                events = []
                for e in conn.execute("select e.*, a.slot_id from appointment_events e join appointments a using(appointment_id) order by e.event_id desc limit 30"):
                    d = json.loads(e["details_json"])
                    slot = slot_text.get(e["slot_id"], "")
                    text = {"HOLD_ACQUIRED": f"held the {slot} slot", "HOLD_RELEASED": f"released the hold on {slot}", "HOLD_EXPIRED": f"hold on {slot} lapsed",
                            "ORIGINAL_ENDED": f"original appointment ({slot}) ended because a new one was confirmed",
                            "RESCHEDULE_CONFIRMED": f"appointment confirmed for {slot}", "APPOINTMENT_CANCELLED": f"appointment at {slot} cancelled"}.get(e["event_type"], e["event_type"])
                    events.append({"time": _hhmmss(parse_operational_timestamp(e["occurred_at"])), "who": label.get(e["shipment_id"], ""), "text": text,
                                   "by": {"DRIVER": "driver", "SYSTEM": "system"}.get(e["actor_type"], e["actor_type"].lower()), "extra": d.get("rank")})
                outbox = []
                for o in conn.execute("select recipient_type, recipient_ref, status, attempts, payload_json from notification_outbox order by outbox_id desc limit 20"):
                    outbox.append({"to": o["recipient_ref"], "role": o["recipient_type"].replace("_", " ").title(), "status": o["status"], "attempts": o["attempts"],
                                   "kind": json.loads(o["payload_json"]).get("kind", "").replace("_", " ").title()})
                return {"events": events, "outbox": outbox, "clock": clock_info()}
            finally:
                conn.close()

        return local_only(request) or await run_in_threadpool(read)

    @app.post("/demo/api/deliver")
    async def deliver(request: Request) -> Any:
        denied = local_only(request)
        if denied:
            return denied

        def go() -> dict[str, Any]:
            conn = db()
            try:
                r = deliver_pending_notifications(conn, demo.clock, lambda n: None)  # a stand-in receiver that accepts everything
                return {"sent": r.sent, "failed": r.failed}
            finally:
                conn.close()

        return await run_in_threadpool(go)


def build_demo_app(workdir: Path | None = None, live_factory: Callable[[], Any] | None = None, policy: Any = None) -> tuple[FastAPI, Any]:
    """The service wired for the demo: fixed clock, in-process cache, a stub or live model, no real channel."""
    import fakeredis

    from setuhaul.api import AppDeps, create_app
    from setuhaul.model_gateway import ModelPolicy
    from setuhaul.stub_model import StubGateway

    work = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="setuhaul-demo-"))
    work.mkdir(parents=True, exist_ok=True)
    db_path = work / "demo.sqlite3"
    clock = FakeClock(parse_operational_timestamp(DEMO_START))
    redis = fakeredis.FakeRedis(decode_responses=True)

    def reset() -> None:
        build_demo_db(db_path)
        redis.flushall()
        clock.set(parse_operational_timestamp(DEMO_START))

    reset()
    state = DemoState(clock=clock, db_path=db_path, reset=reset, live_factory=live_factory)
    deps = AppDeps(
        db_path=db_path, redis=redis, webhook_secret=os.urandom(16), coordinator_tokens={}, fallback_path=work / "fallback.jsonl",
        policy=policy or ModelPolicy(provider="stub", model_id="stub"), clock=clock, demo=state,
        gateway_factory=lambda: live_factory() if (state.model_mode == "live" and live_factory) else StubGateway())
    shutil.rmtree(work / "unused", ignore_errors=True)
    return create_app(deps), deps
