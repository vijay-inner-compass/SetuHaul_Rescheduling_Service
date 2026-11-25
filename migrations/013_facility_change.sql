-- A driver's request to move a load to another facility, decided by the destination warehouse. Extension table only.
CREATE TABLE facility_change_requests (
    request_id TEXT PRIMARY KEY,
    shipment_id TEXT NOT NULL,
    driver_id TEXT NOT NULL,
    -- Where the load was headed when it was asked for: the original is never lost.
    from_facility_id TEXT NOT NULL,
    to_facility_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING','APPROVED','REJECTED','EXPIRED')),
    decided_by TEXT,
    -- Why it was closed without moving the load, when it was not a person's refusal.
    reason TEXT,
    -- After approval: WAITING for a booking at the new site, or BOOKED. NULL until the follow-up has run.
    follow_up TEXT CHECK (follow_up IN ('WAITING','BOOKED')),
    created_at TEXT NOT NULL,
    decided_at TEXT,
    expires_at TEXT NOT NULL,
    FOREIGN KEY (shipment_id) REFERENCES shipments(shipment_id),
    FOREIGN KEY (driver_id) REFERENCES drivers(driver_id),
    FOREIGN KEY (from_facility_id) REFERENCES facilities(facility_id),
    FOREIGN KEY (to_facility_id) REFERENCES facilities(facility_id)
);
-- One open request per load.
CREATE UNIQUE INDEX ux_facility_change_open ON facility_change_requests(shipment_id) WHERE status='PENDING';
