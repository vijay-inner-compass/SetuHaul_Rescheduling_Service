"""Shared set-up for the warehouse exception tests: two docks open on one day and a booked shipment."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from setuhaul.db import parse_operational_timestamp as ts
from tests.auth_helpers import World, build_world
from tests.shipment_helpers import NOW, create, open_av, query, slot_at

D1, D2 = "DOCK-JAI-D1", "DOCK-JAI-D2"


def appointment_of(world: World, shipment_id: str) -> str:
    return str(query(world, "select appointment_id from appointments where shipment_id=? and is_current=1", shipment_id)[0][0])


def booked_world(tmp_path: Path, to_time: str = "13:00") -> tuple[World, str, str]:
    """Two standard docks open on DAY and one shipment booked on D1 at 09:00."""
    world = build_world(tmp_path, FakeClock(ts(NOW)))
    open_av(world, dock_ids=[D1, D2], to_time=to_time)
    shipment_id = create(world, slot_at(world, 9, D1))
    return world, shipment_id, appointment_of(world, shipment_id)


def run(world: World, fn: Any, **kwargs: Any) -> Any:
    """Call a service function on its own connection, with the world's clock."""
    conn = open_connection(path=world.path)
    try:
        return fn(conn, clock=world.clock, **kwargs)
    finally:
        conn.close()
