"""A booked shipment with its pass and a gate guard, for the gate tests. The slot is 28 Nov 12:00-13:00 IST."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import fakeredis

from setuhaul import gate
from setuhaul.clock import FakeClock
from setuhaul.db import open_connection
from setuhaul.db import parse_operational_timestamp as ts
from tests.auth_helpers import GURUGRAM, JAIPUR, World, add_user, build_world
from tests.shipment_helpers import NOW, create_with_code, open_av, query, slot_at

ARRIVE = "2026-11-28T11:45:00+05:30"


class Yard:
    def __init__(self, tmp_path: Path, arrive: bool = True) -> None:
        self.world: World = build_world(tmp_path, FakeClock(ts(NOW)))
        conn = open_connection(path=self.world.path)
        self.world.users["guard"] = add_user(conn, self.world.clock, "guard@x.com", "SECURITY", (JAIPUR,))  # type: ignore[arg-type]
        self.world.users["guard_b"] = add_user(conn, self.world.clock, "guardb@x.com", "SECURITY", (GURUGRAM,))  # type: ignore[arg-type]
        conn.close()
        self.redis = fakeredis.FakeRedis(decode_responses=True)
        open_av(self.world, to_time="17:00")
        self.shipment_id, self.code = create_with_code(self.world, slot_at(self.world, 12))
        self.registration = str(query(self.world, "select registration_number from vehicles where vehicle_id='VEH001'")[0][0])
        self.licence = str(query(self.world, "select licence_number from drivers where driver_id='DRV001'")[0][0])
        if arrive:
            self.world.clock.set(ts(ARRIVE))  # type: ignore[attr-defined]

    def request(self, **over: str) -> gate.GateRequest:
        values = {"code": self.code, "vehicle_registration": self.registration, "licence_number": self.licence,
                  "request_key": "key-1"} | over
        return gate.GateRequest(**values)

    def call(self, name: str, who: str = "guard", **kwargs: Any) -> Any:
        conn = open_connection(path=self.world.path)
        try:
            fn = getattr(gate, name)
            if name in ("validate", "check_in"):
                return fn(conn, self.redis, actor=self.world.users[who], data=kwargs.pop("data", None) or self.request(**kwargs),
                          clock=self.world.clock)
            return fn(conn, actor=self.world.users[who], clock=self.world.clock, **kwargs)
        finally:
            conn.close()

    def one(self, sql: str, *args: object) -> Any:
        return query(self.world, sql, *args)[0]
