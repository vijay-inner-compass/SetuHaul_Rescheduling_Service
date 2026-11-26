"""Shared set-up for the assigned-shipment tests."""

from __future__ import annotations

import sqlite3
from typing import Any

from setuhaul.auth import shipments
from setuhaul.db import open_connection

ADMIN_ID = "USR-SHIPADMIN"


def mark_unconfirmed(conn: sqlite3.Connection, shipment_id: str, created_by: str = ADMIN_ID) -> None:
    """Give a supplied shipment a pending assignment, as the Shipments tab would for a new one."""
    conn.execute("insert or ignore into users(user_id, email, role, status, display_name, invited_by, created_at) "
                 "values (?,?,'ADMIN','ACTIVE','Ship Admin',NULL,'2026-08-04T06:00:00+05:30')", (ADMIN_ID, "shipadmin@x.com"))
    conn.execute("insert into shipment_assignments(shipment_id, created_by, created_at) values (?,?,'2026-08-04T06:00:00+05:30')",
                 (shipment_id, created_by))
    conn.commit()


def confirm_row(conn: sqlite3.Connection, shipment_id: str) -> None:
    conn.execute("update shipment_assignments set confirmed_at='2026-08-04T07:00:00+05:30' where shipment_id=?", (shipment_id,))
    conn.commit()


# ---- a world on a date after the sample data -----------------------------------------------------
NOW = "2026-11-27T10:00:00+05:30"
DAY = "2026-11-28"
D1 = "DOCK-JAI-D1"


def open_av(world: Any, who: str = "wh_a", **over: Any) -> Any:
    values: dict[str, Any] = {"facility_id": "FAC-JAI-01", "first_date": DAY, "last_date": DAY, "dock_ids": [D1],
                              "from_time": "09:00", "to_time": "13:00", "crews": 4} | over
    conn = open_connection(path=world.path)
    try:
        return shipments.open_availability(conn, actor=world.users[who], data=shipments.Availability(**values), clock=world.clock)
    finally:
        conn.close()


def slot_at(world: Any, hour: int, dock: str = D1, day: str = DAY) -> str:
    found = query(world, "select slot_id from appointment_slots where dock_id=? and slot_start_ts=?",
                  dock, f"{day}T{hour:02d}:00:00+05:30")
    return str(found[0]["slot_id"])


def new_shipment(world: Any, slot_id: str, **over: Any) -> Any:
    values: dict[str, Any] = {
        "order_number": "ORD-261127-001", "driver_id": "DRV001", "vehicle_id": "VEH001", "origin_name": "Test Plant",
        "origin_city": "Neemrana", "facility_id": "FAC-JAI-01", "customer_name": "Acme", "product_category": "FMCG",
        "load_weight_kg": 12000, "pallet_count": 10, "required_dock_type": "STANDARD", "temperature_control_required": False,
        "priority_code": "NORMAL", "expected_unload_min": 50, "planned_departure": "2026-11-27T20:00:00",
        "slot_id": slot_id, "latest_arrival": None, "penalty_applies": False} | over
    return shipments.NewShipment(**values)


def create(world: Any, slot_id: str, who: str = "wh_a", **over: Any) -> str:
    return create_with_code(world, slot_id, who, **over)[0]


def create_with_code(world: Any, slot_id: str, who: str = "wh_a", **over: Any) -> tuple[str, str]:
    conn = open_connection(path=world.path)
    try:
        return shipments.create_shipment(conn, actor=world.users[who], data=new_shipment(world, slot_id, **over), clock=world.clock)
    finally:
        conn.close()


def query(world: Any, sql: str, *args: object) -> list[sqlite3.Row]:
    conn = open_connection(path=world.path)
    try:
        rows = conn.execute(sql, args).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def count(world: Any, table: str, where: str = "1=1") -> int:
    return int(query(world, f"select count(*) from {table} where {where}")[0][0])
