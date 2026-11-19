-- Phase 4: inputs the supplied snapshot does not contain. Empty by default, which makes
-- feasibility return INCOMPLETE_FEASIBILITY_DATA instead of assuming availability.
-- Rows must carry a `source` so scenario fixtures are never mistaken for SetuHaul data.

CREATE TABLE labour_windows (
    window_id INTEGER PRIMARY KEY AUTOINCREMENT,
    facility_id TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    available_crews INTEGER NOT NULL CHECK (available_crews >= 0),
    equipment_available INTEGER NOT NULL DEFAULT 1 CHECK (equipment_available IN (0,1)),
    source TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    CHECK (window_end > window_start),
    FOREIGN KEY (facility_id) REFERENCES facilities(facility_id)
);

-- A row means "commitment status is known". latest_arrival NULL = known, no deadline.
CREATE TABLE consignee_commitments (
    shipment_id TEXT PRIMARY KEY,
    latest_arrival TEXT,
    penalty_applies INTEGER NOT NULL DEFAULT 0 CHECK (penalty_applies IN (0,1)),
    exceptional INTEGER NOT NULL DEFAULT 0 CHECK (exceptional IN (0,1)),
    source TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (shipment_id) REFERENCES shipments(shipment_id)
);
