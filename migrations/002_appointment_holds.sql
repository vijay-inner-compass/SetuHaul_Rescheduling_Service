-- Phase 5: holds live INSIDE the appointment capacity boundary.
-- SQLite cannot alter a CHECK constraint, so `appointments` is rebuilt with HELD added to the
-- status list and to the active-slot unique index. The runner turns foreign keys off around this
-- script and verifies foreign_key_check afterwards. The views that read `appointments` must be
-- dropped first (RENAME validates view bodies) and are recreated below.

-- Views that read appointments must be dropped before the table is rebuilt.
DROP VIEW v_slot_availability;
DROP VIEW v_inbound_operational_state;
-- The old unique indexes are replaced below, with HELD included.
DROP INDEX ux_active_appointment_per_slot;
DROP INDEX ux_current_active_appointment_per_shipment;

-- The new appointments table: the same columns, but HELD is now a valid status.
CREATE TABLE appointments_new (
    -- The hold's appointment row.
    appointment_id TEXT PRIMARY KEY,
    shipment_id TEXT NOT NULL,
    slot_id TEXT NOT NULL,
    -- Allowed statuses; HELD is the new one.
    appointment_status TEXT NOT NULL
        CHECK (appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS','COMPLETED','CANCELLED','NO_SHOW','REJECTED','HELD')),
    -- Who made the booking.
    booking_source TEXT NOT NULL
        CHECK (booking_source IN ('PLANNER','DRIVER_CHAT','WAREHOUSE','SCHEDULING_TOOL','MANUAL_OVERRIDE')),
    -- 1 for a shipment's current appointment; a hold is never current.
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1)),
    booked_at TEXT NOT NULL,
    confirmed_at TEXT,
    cancelled_at TEXT,
    cancellation_reason TEXT,
    -- For a confirmed reschedule: the appointment it replaced.
    replaced_appointment_id TEXT,
    warehouse_confirmation_ref TEXT,
    updated_at TEXT NOT NULL,
    -- References: shipment, slot and the replaced appointment.
    FOREIGN KEY (shipment_id) REFERENCES shipments(shipment_id),
    FOREIGN KEY (slot_id) REFERENCES appointment_slots(slot_id),
    FOREIGN KEY (replaced_appointment_id) REFERENCES appointments(appointment_id)
);
-- Copy every existing row unchanged.
INSERT INTO appointments_new SELECT * FROM appointments;
-- Remove the old table.
DROP TABLE appointments;
-- Give the new table the old name.
ALTER TABLE appointments_new RENAME TO appointments;

-- One active appointment (confirmed, requested, in progress OR held) per slot. Holds and
-- bookings therefore collide on the same constraint, whoever writes them.
-- THE capacity lock: at most one active row per slot, now counting holds. Holds and bookings by anyone collide here.
CREATE UNIQUE INDEX ux_active_appointment_per_slot
ON appointments(slot_id)
WHERE appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS','HELD');

-- Unchanged predicate: a held row is never is_current, so the original appointment stays current.
-- A shipment has at most one current appointment.
CREATE UNIQUE INDEX ux_current_active_appointment_per_shipment
ON appointments(shipment_id)
WHERE is_current = 1
  AND appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS');

-- At most one hold per shipment.
-- A shipment can hold at most one slot.
CREATE UNIQUE INDEX ux_one_hold_per_shipment
ON appointments(shipment_id)
WHERE appointment_status = 'HELD';

-- Recreate the availability view: each slot as AVAILABLE, HELD, OCCUPIED or its blocked status.
CREATE VIEW v_slot_availability AS
SELECT
    sl.slot_id,
    sl.facility_id,
    d.dock_code,
    d.dock_type,
    d.supports_refrigerated,
    d.max_vehicle_weight_kg,
    sl.slot_start_ts,
    sl.slot_end_ts,
    CASE
        WHEN sl.slot_status <> 'OPEN' THEN sl.slot_status
        WHEN a.appointment_status = 'HELD' THEN 'HELD'
        WHEN a.appointment_id IS NOT NULL THEN 'OCCUPIED'
        ELSE 'AVAILABLE'
    END AS availability_status,
    a.appointment_id,
    a.shipment_id,
    a.appointment_status
FROM appointment_slots sl
JOIN docks d ON d.dock_id = sl.dock_id
LEFT JOIN appointments a
    ON a.slot_id = sl.slot_id
   AND a.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS','HELD');

-- Recreate the operational view: each shipment with its ETA, appointment, slot, dock and gate state.
CREATE VIEW v_inbound_operational_state AS
SELECT
    s.shipment_id,
    s.driver_id,
    s.vehicle_id,
    s.destination_facility_id,
    s.priority_code,
    s.required_dock_type,
    s.temperature_control_required,
    s.load_weight_kg,
    s.expected_unload_min,
    s.current_status,
    le.effective_eta_ts,
    le.eta_source,
    le.eta_confidence,
    ap.appointment_id,
    sl.slot_id,
    sl.slot_start_ts,
    sl.slot_end_ts,
    d.dock_code AS planned_dock_code,
    fc.gate_in_ts,
    fc.queue_state,
    fc.queue_position,
    ad.dock_code AS actual_dock_code
FROM shipments s
JOIN v_latest_eta le ON le.shipment_id = s.shipment_id
LEFT JOIN appointments ap
    ON ap.shipment_id = s.shipment_id
   AND ap.is_current = 1
   AND ap.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
LEFT JOIN appointment_slots sl ON sl.slot_id = ap.slot_id
LEFT JOIN docks d ON d.dock_id = sl.dock_id
LEFT JOIN facility_checkins fc ON fc.shipment_id = s.shipment_id
LEFT JOIN docks ad ON ad.dock_id = fc.actual_dock_id;

-- Hold metadata extends the appointment row; it is NOT the capacity lock.
-- expires_epoch lets SQL compare instants without comparing timestamp strings.
-- Extra facts about a hold row.
CREATE TABLE appointment_hold_meta (
    appointment_id TEXT PRIMARY KEY,
    -- Who owns the hold.
    driver_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    -- The menu it came from.
    option_set_id TEXT NOT NULL,
    -- Expiry as text.
    expires_at TEXT NOT NULL,
    -- Expiry as whole seconds, for exact comparison.
    expires_epoch INTEGER NOT NULL,
    -- Hash identifying the request that made it.
    request_fingerprint TEXT NOT NULL,
    -- When it ended; empty while it is live.
    released_at TEXT,
    -- Why it ended: expired, cancelled, superseded, system or confirmed.
    release_reason TEXT CHECK (release_reason IS NULL OR release_reason IN
        ('EXPIRED','DRIVER_CANCELLED','SUPERSEDED','SYSTEM','CONFIRMED')),
    FOREIGN KEY (appointment_id) REFERENCES appointments(appointment_id),
    FOREIGN KEY (driver_id) REFERENCES drivers(driver_id),
    FOREIGN KEY (thread_id) REFERENCES chat_threads(thread_id)
);
-- Fast lookup of overdue holds for the sweep.
CREATE INDEX ix_hold_meta_expiry ON appointment_hold_meta(expires_epoch);
