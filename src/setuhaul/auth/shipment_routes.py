"""Warehouse routes behind the Shipments tab: availability, assigned shipments and what can be done to one."""

# Lazy annotations.
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from setuhaul import assign, facility_change, passes
from setuhaul.auth import shipments
from setuhaul.auth.authz import require_role
from setuhaul.auth.http import reject
from setuhaul.auth.users import User
from setuhaul.config import load_settings
from setuhaul.db import open_connection
from setuhaul.identity import AuthError
from setuhaul.memory import ConversationMemoryStore

_CONFLICT = {"DUPLICATE_ORDER_NUMBER", "SLOT_NOT_AVAILABLE", "ALREADY_CONFIRMED", "NOT_LOCKED", "VISIT_IN_PROGRESS", "VISIT_CLOSED",
             "NO_LIVE_PASS", "SHIPMENT_FINISHED", "ORIGINAL_IN_PROGRESS", "SAME_FACILITY"}



# What the person at the screen is told for each refusal.
_MESSAGES = {
    "FACILITY_NOT_FOUND": "That facility is not one of yours.",
    "PARTNER_FACILITY": "Partner warehouses are booked by email, not here.",
    "DOCK_NOT_FOUND": "Choose at least one dock of that facility.",
    "INVALID_TIMES": "The times must be in order and inside the facility's opening hours.",
    "BAD_ORDER_NUMBER": "The order number is ORD followed by at least six digits.",
    "DUPLICATE_ORDER_NUMBER": "That order number is already in use.",
    "DRIVER_NOT_FOUND": "That driver is not one you can assign.",
    "DRIVER_NOT_ELIGIBLE": "That driver is not approved, active and licensed.",
    "VEHICLE_CARRIER_MISMATCH": "Choose an active vehicle of the driver's carrier.",
    "SLOT_NOT_AVAILABLE": "That dock slot has been taken or has passed. Reload and choose another.",
    "INVALID_DEPARTURE": "The planned departure must be before the dock slot.",
    "LOAD_DOES_NOT_FIT": "This load cannot use that slot",
    "SHIPMENT_NOT_FOUND": "That shipment no longer exists for you. Reload the page.",
    "ALREADY_CONFIRMED": "The driver has already confirmed it, so it cannot be changed here.",
    "NOT_LOCKED": "That shipment is not locked.",
    "VISIT_IN_PROGRESS": "The truck is already inside, so the pass cannot be replaced.",
    "VISIT_CLOSED": "That visit has ended.",
    "NO_LIVE_PASS": "There is no pass to revoke.",
    "NO_APPOINTMENT": "The shipment has no booked slot. Book one first.",
    "REASON_REQUIRED": "Say why the pass is being revoked.",
    "SAME_FACILITY": "The shipment is already assigned to that warehouse.",
    "SHIPMENT_FINISHED": "That shipment is already finished or cancelled.",
    "ORIGINAL_IN_PROGRESS": "The truck is already at the facility, so its booking cannot be changed.",
}
# Why the booking rules refused a slot.
_WHY = {
    "DOCK_TYPE_MISMATCH": "the dock type does not match", "REFRIGERATION_REQUIRED": "the dock is not refrigerated",
    "HEAVY_DOCK_REQUIRED": "it needs the heavy dock", "WEIGHT_EXCEEDS_DOCK_LIMIT": "it is too heavy for the dock",
    "UNLOAD_EXCEEDS_SLOT": "unloading takes longer than the slot", "LABOUR_UNAVAILABLE": "no crew is free then",
    "EQUIPMENT_UNAVAILABLE": "no equipment is available then", "labour_availability": "no labour is recorded for that day",
    "AFTER_LAST_NEW_START": "it starts after the facility's last start time", "OUTSIDE_FACILITY_HOURS": "it is outside opening hours",
    "DOCK_UNAVAILABLE": "the dock is out of service", "DOCK_EVENT_BLOCK": "the dock is blocked then",
    "RELEASE_AFTER_SLOT_START": "the truck cannot arrive by then",
}


def _message(exc: AuthError) -> str | None:
    if isinstance(exc, shipments.BadDates):
        return exc.message
    text = _MESSAGES.get(exc.code)
    if text is None or not isinstance(exc, (shipments.DoesNotFit, assign.Refused)):
        return text
    return f"{text}: {'; '.join(_WHY.get(r, r) for r in exc.reasons)}."


class Revocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str


class DriverChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    driver_id: str
    vehicle_id: str


class WarehouseChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facility_id: str
    slot_id: str | None = None


class SlotAllocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot_id: str


def _status(code: str) -> int:
    if code.endswith("_NOT_FOUND"):
        return 404
    return 409 if code in _CONFLICT else 422


def register_shipment_routes(app: FastAPI, deps: Any) -> None:
    def run(rid: str, work: Callable[[sqlite3.Connection], Response], readonly: bool = False) -> Response:
        try:
            conn = open_connection(path=deps.db_path, readonly=readonly)
        except (sqlite3.Error, FileNotFoundError):
            return reject(rid, 503, "DATABASE_UNAVAILABLE")
        try:
            return work(conn)
        except AuthError as exc:
            return JSONResponse({"status": "REJECTED", "reason_code": exc.code, "message": _message(exc), "request_id": rid},
                                status_code=_status(exc.code))
        except sqlite3.Error:
            return reject(rid, 503, "DATABASE_UNAVAILABLE")
        finally:
            conn.close()

    def client() -> Any:
        from setuhaul.api import telegram_in_force

        return telegram_in_force(deps)[1]

    def ok(rid: str, status: int = 200, **fields: Any) -> Response:
        return JSONResponse({"status": "OK", **fields, "request_id": rid}, status_code=status)

    @app.get("/v1/warehouse/shipments")
    async def listed(request: Request, user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id
        return await run_in_threadpool(run, rid, lambda conn: ok(rid, shipments=shipments.list_shipments(conn, user)), True)

    @app.post("/v1/warehouse/availability")
    async def availability(request: Request, body: shipments.Availability,
                           user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id

        def write(conn: sqlite3.Connection) -> Response:
            result = shipments.open_availability(conn, actor=user, data=body, clock=deps.clock)
            return ok(rid, **result.model_dump())

        return await run_in_threadpool(run, rid, write)

    @app.post("/v1/warehouse/shipments")
    async def create(request: Request, body: shipments.NewShipment,
                     user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id

        def write(conn: sqlite3.Connection) -> Response:
            shipment_id, code = shipments.create_shipment(conn, actor=user, data=body, clock=deps.clock)
            # The code goes to the driver only, never into the response. A failed send is shown on the screen, not an error here.
            passes.deliver(conn, client(), shipment_id, code, deps.clock)
            return ok(rid, 201, shipment_id=shipment_id)

        return await run_in_threadpool(run, rid, write)

    @app.post("/v1/warehouse/shipments/{shipment_id}/driver")
    async def driver(request: Request, shipment_id: str, body: DriverChange,
                     user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id

        def write(conn: sqlite3.Connection) -> Response:
            code = shipments.change_driver(conn, actor=user, shipment_id=shipment_id, driver_id=body.driver_id,
                                           vehicle_id=body.vehicle_id, clock=deps.clock)
            passes.deliver(conn, client(), shipment_id, code, deps.clock)
            return ok(rid, shipment_id=shipment_id)

        return await run_in_threadpool(run, rid, write)

    @app.post("/v1/warehouse/shipments/{shipment_id}/pass")
    async def new_pass(request: Request, shipment_id: str,
                       user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id

        def write(conn: sqlite3.Connection) -> Response:
            sent = passes.send_new(conn, client(), actor=user, shipment_id=shipment_id, clock=deps.clock)
            return ok(rid, shipment_id=shipment_id, delivered=sent)

        return await run_in_threadpool(run, rid, write)

    @app.post("/v1/warehouse/shipments/{shipment_id}/pass/revoke")
    async def revoke_pass(request: Request, shipment_id: str, body: Revocation,
                          user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id

        def write(conn: sqlite3.Connection) -> Response:
            passes.revoke(conn, actor=user, shipment_id=shipment_id, reason=body.reason, clock=deps.clock)
            return ok(rid, shipment_id=shipment_id)

        return await run_in_threadpool(run, rid, write)

    def action(name: str, work: Callable[..., None]) -> None:
        @app.post(f"/v1/warehouse/shipments/{{shipment_id}}/{name}")
        async def handler(request: Request, shipment_id: str,
                          user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
            rid = request.state.request_id

            def write(conn: sqlite3.Connection) -> Response:
                work(conn, actor=user, shipment_id=shipment_id, clock=deps.clock)
                return ok(rid, shipment_id=shipment_id)

            return await run_in_threadpool(run, rid, write)

    def notify_driver(conn: sqlite3.Connection, shipment_id: str, key: str, **fields: str) -> None:
        try:
            row = conn.execute("select driver_id from shipments where shipment_id=?", (shipment_id,)).fetchone()
            if row is None:
                return
            driver_id = str(row["driver_id"])
            memory = None if deps.redis is None else ConversationMemoryStore(conn, deps.redis, deps.clock, load_settings())
            facility_change._tell(conn, deps.clock, memory, driver_id, facility_change._thread_of(conn, driver_id),
                                  shipment_id, key, happened_at=None, **fields)
            conn.commit()
        except Exception:  # noqa: BLE001 - best-effort driver notice after committed write  # noqa: BLE001 - the notice is best effort; the warehouse change is already saved and must not fail with it
            conn.rollback()

    @app.post("/v1/warehouse/shipments/{shipment_id}/warehouse")
    async def warehouse(request: Request, shipment_id: str, body: WarehouseChange,
                        user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id

        def write(conn: sqlite3.Connection) -> Response:
            order, facility_name = shipments.change_warehouse(
                conn, actor=user, shipment_id=shipment_id, facility_id=body.facility_id, clock=deps.clock, slot_id=body.slot_id)
            if body.slot_id:
                passes.send_pending(conn, client(), deps.clock)
                appt = passes.current_appointment(conn, shipment_id)
                if appt is not None:
                    dock_row = conn.execute("select dock_code from docks where dock_id=?", (appt["dock_id"],)).fetchone()
                    from setuhaul.db import IST, parse_operational_timestamp
                    start_ist = parse_operational_timestamp(appt["slot_start_ts"]).astimezone(IST)
                    notify_driver(conn, shipment_id, "booking_assigned",
                                  dock=str(dock_row["dock_code"]) if dock_row else "",
                                  date=f"{start_ist:%d %b %Y}", start=f"{start_ist:%H:%M}")
            else:
                notify_driver(conn, shipment_id, "warehouse_changed", order=order, facility=facility_name)
            return ok(rid, shipment_id=shipment_id, facility_id=body.facility_id)

        return await run_in_threadpool(run, rid, write)

    @app.post("/v1/warehouse/shipments/{shipment_id}/slot")
    async def slot(request: Request, shipment_id: str, body: SlotAllocation,
                   user: Annotated[User, Depends(require_role("WAREHOUSE_AUTHORITY", "ADMIN"))]) -> Response:
        rid = request.state.request_id

        def write(conn: sqlite3.Connection) -> Response:
            dock, start = shipments.allocate_slot(conn, actor=user, shipment_id=shipment_id, slot_id=body.slot_id,
                                                  clock=deps.clock)
            passes.send_pending(conn, client(), deps.clock)
            notify_driver(conn, shipment_id, "booking_assigned", dock=dock, date=f"{start:%d %b %Y}", start=f"{start:%H:%M}")
            return ok(rid, shipment_id=shipment_id, slot_id=body.slot_id, dock_code=dock, start=start.isoformat())

        return await run_in_threadpool(run, rid, write)
    action("cancel", shipments.cancel_shipment)
    action("unlock", shipments.unlock)
