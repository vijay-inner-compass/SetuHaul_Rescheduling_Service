-- How many crews a warehouse has, for creating its slots automatically. No row: nothing is created for it.
CREATE TABLE facility_crews (
    facility_id TEXT PRIMARY KEY,
    crews INTEGER NOT NULL CHECK (crews >= 1),
    -- The user who typed the number.
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (facility_id) REFERENCES facilities(facility_id)
);
