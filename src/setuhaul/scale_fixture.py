"""Generated scale fixture: about 360 daily loads. NOT SetuHaul data.

Every generated row is marked: ids start with ``GEN-``, customer and names say GENERATED SCALE FIXTURE, and
labour/consignee evidence carries source ``GENERATED_SCALE_FIXTURE``. It exists to put realistic volume
on the search and booking paths; it says nothing about SetuHaul's real network or demand.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from setuhaul.db import IST, make_working_db, open_connection
from setuhaul.identity import provision_identities

DAY = datetime(2026, 8, 4, tzinfo=IST)
SOURCE = "GENERATED_SCALE_FIXTURE"
BURST_ETA = "10:00"


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def make_scale_fixture(dest: Path, loads: int = 360, seed: int = 7, burst_group: int = 12) -> dict[str, Any]:
    rng = random.Random(seed)
    path = make_working_db(dest)
    conn = open_connection(path=path)
    now = _iso(DAY.replace(hour=5))
    vtype = conn.execute("select vehicle_type_code from vehicle_types order by 1 limit 1").fetchone()[0]

    # capacity: extra docks and hourly slots on top of the supplied two facilities
    plan = {"FAC-JAI-01": (30, 6, 22), "FAC-GGN-01": (20, 9, 21)}
    slots: list[tuple[str, str, str, datetime]] = []  # slot_id, facility, dock_id, start
    docks: dict[str, tuple[str, str, int]] = {}  # dock_id -> (facility, type, max_kg)
    for fac, (n, first, last) in plan.items():
        for i in range(n):
            kind = "REEFER" if i % 8 == 0 else ("HEAVY" if i % 8 == 1 else "STANDARD")
            dock_id = f"GEN-{fac[4:7]}-D{i + 20:02d}"
            max_kg = {"REEFER": 22000, "HEAVY": 35000, "STANDARD": 25000}[kind]
            conn.execute("insert into docks (dock_id, facility_id, dock_code, dock_type, supports_refrigerated, max_vehicle_weight_kg, dock_status) "
                         "values (?,?,?,?,?,?, 'ACTIVE')", (dock_id, fac, f"G{i + 20:02d}", kind, int(kind == "REEFER"), max_kg))
            docks[dock_id] = (fac, kind, max_kg)
            for h in range(first, last):
                start = DAY.replace(hour=h)
                slot_id = f"GEN-SLOT-{len(slots) + 1:04d}"
                conn.execute("insert into appointment_slots (slot_id, facility_id, dock_id, slot_start_ts, slot_end_ts, slot_status, created_at) "
                             "values (?,?,?,?,?, 'OPEN',?)", (slot_id, fac, dock_id, _iso(start), _iso(start + timedelta(hours=1)), now))
                slots.append((slot_id, fac, dock_id, start))

    taken: set[str] = {r[0] for r in conn.execute("select slot_id from appointments where appointment_status in ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS','HELD')")}
    made: dict[str, Any] = {"shipments": 0, "appointments": 0, "burst": []}
    for i in range(1, loads + 1):
        did, vid, sid = f"GEN-DRV{i:04d}", f"GEN-VEH{i:04d}", f"GEN-SHP{i:04d}"
        fac = "FAC-JAI-01" if i <= int(loads * 2 / 3) else "FAC-GGN-01"
        is_burst = i <= burst_group  # identical, unscheduled, standard loads at JAI: they will all want the same slot
        kind = "STANDARD" if is_burst else rng.choices(["STANDARD", "REEFER", "HEAVY"], [85, 8, 7])[0]
        weight = rng.randint(3000, 18000) if kind != "HEAVY" else rng.randint(26000, 30000)
        unload = 45 if is_burst else rng.choice([30, 45, 60])
        eta = DAY.replace(hour=10, minute=0) if is_burst else DAY.replace(hour=rng.randint(7, 19), minute=rng.choice([0, 15, 30, 45]))
        conn.execute("insert into drivers (driver_id, carrier_id, driver_name, phone, licence_number, home_base_city, driver_status) "
                     "values (?, 'CAR001', ?, ?, ?, 'Generated', 'ACTIVE')", (did, f"Generated Driver {i:04d}", f"+91-91{i:08d}", f"GENLIC{i:04d}"))
        conn.execute("insert into vehicles (vehicle_id, carrier_id, vehicle_type_code, registration_number, capacity_kg, refrigeration_capable, active_flag) "
                     "values (?, 'CAR001', ?, ?, 32000, ?, 1)", (vid, vtype, f"GEN{i:04d}", int(kind == "REEFER")))
        conn.execute(
            "insert into shipments (shipment_id, order_reference, carrier_id, driver_id, vehicle_id, origin_name, origin_city, destination_facility_id, "
            "customer_name, product_category, load_weight_kg, pallet_count, required_dock_type, temperature_control_required, priority_code, "
            "planned_departure_ts, original_eta_ts, latest_eta_ts, expected_unload_min, current_status, created_at, updated_at) "
            "values (?,?, 'CAR001',?,?, 'Generated origin','Generated', ?, 'GENERATED SCALE FIXTURE','Generated goods',?,10,?,?,?,?,?,?,?, 'IN_TRANSIT',?,?)",
            (sid, f"GEN-ORD-{i:04d}", did, vid, fac, weight, kind, int(kind == "REEFER"),
             rng.choice(["LOW", "NORMAL", "NORMAL", "HIGH", "CRITICAL"]), _iso(eta - timedelta(hours=3)), _iso(eta), _iso(eta), unload, now, now))
        conn.execute("insert into chat_threads (thread_id, driver_id, shipment_id, opened_at, thread_status, thread_intent) "
                     "values (?,?,?,?, 'OPEN','UNKNOWN')", (f"GEN-THR{i:04d}", did, sid, now))
        conn.execute("insert into consignee_commitments (shipment_id, latest_arrival, penalty_applies, exceptional, source, recorded_at) "
                     "values (?, NULL, 0, 0, ?, ?)", (sid, SOURCE, now))
        made["shipments"] += 1
        if is_burst:
            made["burst"].append(did)
        elif rng.random() < 0.7:  # most loads already hold a confirmed appointment
            for slot_id, sfac, dock_id, start in slots:
                _f, k, mx = docks[dock_id]
                if sfac == fac and slot_id not in taken and start >= eta and (k == kind or (kind == "STANDARD" and k == "STANDARD")) and weight <= mx:
                    conn.execute("insert into appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source, is_current, "
                                 "booked_at, updated_at) values (?,?,?, 'CONFIRMED','PLANNER',1,?,?)", (f"GEN-APT{i:04d}", sid, slot_id, now, now))
                    taken.add(slot_id)
                    made["appointments"] += 1
                    break
    for fac, crews in (("FAC-JAI-01", 60), ("FAC-GGN-01", 40)):
        conn.execute("insert into labour_windows (facility_id, window_start, window_end, available_crews, equipment_available, source, recorded_at) "
                     "values (?,?,?,?,1,?,?)", (fac, _iso(DAY.replace(hour=5)), _iso(DAY.replace(hour=23)), crews, SOURCE, now))
    conn.commit()
    provision_identities(conn)
    summary = {"path": str(path), "loads": loads, "docks_added": len(docks), "slots_added": len(slots), **made,
               "slots_total": conn.execute("select count(*) from appointment_slots").fetchone()[0],
               "appointments_active": conn.execute("select count(*) from appointments where appointment_status in ('CONFIRMED','PENDING_CONFIRMATION','IN_PROGRESS')").fetchone()[0]}
    conn.close()
    return summary


def generated_driver_phone(i: int) -> str:
    return f"+91-91{i:08d}"


def open_ro(path: Path) -> sqlite3.Connection:
    return open_connection(path=path, readonly=True)
