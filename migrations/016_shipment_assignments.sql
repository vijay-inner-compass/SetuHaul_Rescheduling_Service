-- Shipments created in the app wait here until the driver sends the order number (decision D100).
-- A shipment with no row, such as every supplied one, needs no confirmation.
CREATE TABLE shipment_assignments (
    shipment_id TEXT PRIMARY KEY,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    -- Empty until the driver has sent the order number.
    confirmed_at TEXT,
    -- Wrong order numbers sent while this was waiting.
    failed_attempts INTEGER NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
    locked_at TEXT,
    FOREIGN KEY (shipment_id) REFERENCES shipments(shipment_id),
    FOREIGN KEY (created_by) REFERENCES users(user_id)
);

-- The shipments a driver may be shown or may act on. Every driver-facing read uses this; warehouse screens read the table.
CREATE VIEW v_driver_shipments AS
SELECT s.* FROM shipments s
WHERE NOT EXISTS (SELECT 1 FROM shipment_assignments a WHERE a.shipment_id = s.shipment_id AND a.confirmed_at IS NULL);

-- A truck's visit to a warehouse (decision D101). One shipment each for now; the link table allows more later.
CREATE TABLE truck_visits (
    truck_visit_id TEXT PRIMARY KEY,
    warehouse_id TEXT NOT NULL REFERENCES facilities(facility_id),
    driver_id TEXT NOT NULL REFERENCES drivers(driver_id),
    vehicle_id TEXT NOT NULL REFERENCES vehicles(vehicle_id),
    carrier_id TEXT NOT NULL REFERENCES carriers(carrier_id),
    status TEXT NOT NULL DEFAULT 'EXPECTED'
        CHECK (status IN ('EXPECTED','CHECKED_IN','OPERATIONS_COMPLETE','CHECKED_OUT','CANCELLED')),
    -- The screen's key for the action that succeeded, so a repeat of the same press is not a second action.
    check_in_key TEXT,
    check_out_key TEXT,
    checked_in_at TEXT,
    completed_at TEXT,
    checked_out_at TEXT,
    created_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE visit_shipments (
    truck_visit_id TEXT NOT NULL REFERENCES truck_visits(truck_visit_id),
    shipment_id TEXT NOT NULL REFERENCES shipments(shipment_id),
    PRIMARY KEY (truck_visit_id, shipment_id)
);

-- Dock Access Passes. Only the SHA-256 of a code is kept, never the code.
CREATE TABLE dock_access_authorizations (
    authorization_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    appointment_id TEXT NOT NULL REFERENCES appointments(appointment_id),
    truck_visit_id TEXT NOT NULL REFERENCES truck_visits(truck_visit_id),
    driver_id TEXT NOT NULL REFERENCES drivers(driver_id),
    vehicle_registration TEXT NOT NULL,
    carrier_id TEXT NOT NULL REFERENCES carriers(carrier_id),
    warehouse_id TEXT NOT NULL REFERENCES facilities(facility_id),
    dock_id TEXT REFERENCES docks(dock_id),
    valid_from_utc TEXT NOT NULL,
    valid_until_utc TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ISSUED'
        CHECK (status IN ('ISSUED','VALIDATED','ACTIVE','COMPLETED','CLOSED','EXPIRED','REVOKED','CANCELLED')),
    issued_at TEXT NOT NULL,
    delivered_at TEXT,
    activated_at TEXT,
    revoked_at TEXT,
    revocation_reason TEXT,
    row_version INTEGER NOT NULL DEFAULT 1
);
-- One live pass for a visit.
CREATE UNIQUE INDEX ux_live_pass_per_visit ON dock_access_authorizations(truck_visit_id)
WHERE status IN ('ISSUED','VALIDATED','ACTIVE','COMPLETED');
