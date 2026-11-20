-- Phase 4: inputs the supplied snapshot does not contain. Empty by default, which makes
-- feasibility return INCOMPLETE_FEASIBILITY_DATA instead of assuming availability.
-- Rows must carry a `source` so scenario fixtures are never mistaken for SetuHaul data.

-- Labour crews and equipment per facility and time window.
CREATE TABLE labour_windows (
    window_id INTEGER PRIMARY KEY AUTOINCREMENT,
    facility_id TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    -- Number of crews; cannot be negative.
    available_crews INTEGER NOT NULL CHECK (available_crews >= 0),
    -- Is equipment available (1) or not (0)?
    equipment_available INTEGER NOT NULL DEFAULT 1 CHECK (equipment_available IN (0,1)),
    -- Where the row came from, so test or generated data is never mistaken for real data.
    source TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    -- A window must end after it starts.
    CHECK (window_end > window_start),
    FOREIGN KEY (facility_id) REFERENCES facilities(facility_id)
);

-- A row means "commitment status is known". latest_arrival NULL = known, no deadline.
-- What the customer expects, per shipment.
CREATE TABLE consignee_commitments (
    shipment_id TEXT PRIMARY KEY,
    -- The latest acceptable arrival; empty means no deadline.
    latest_arrival TEXT,
    -- Does missing it cost money?
    penalty_applies INTEGER NOT NULL DEFAULT 0 CHECK (penalty_applies IN (0,1)),
    -- Is it an exceptional delivery needing approval to miss?
    exceptional INTEGER NOT NULL DEFAULT 0 CHECK (exceptional IN (0,1)),
    source TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (shipment_id) REFERENCES shipments(shipment_id)
);
