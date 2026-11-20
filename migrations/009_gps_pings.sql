-- GPS-assisted ETA: append-only driver location pings per shipment.
CREATE TABLE gps_pings (
    -- Monotonic id; breaks ties between pings recorded at the same instant.
    ping_id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- The shipment the driver was carrying when the ping was sent.
    shipment_id TEXT NOT NULL REFERENCES shipments(shipment_id),
    -- Position in decimal degrees; the CHECKs reject impossible coordinates at the database level.
    lat REAL NOT NULL CHECK (lat BETWEEN -90 AND 90),
    lon REAL NOT NULL CHECK (lon BETWEEN -180 AND 180),
    -- When the device took the fix, and when this service received it (ISO-8601 with offset).
    recorded_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    -- Where the ping came from (for example the provider or channel name).
    source TEXT NOT NULL
);
-- Serves "latest ping for a shipment": newest recorded_at first, ping_id as tie-break.
CREATE INDEX idx_gps_pings_latest ON gps_pings (shipment_id, recorded_at DESC, ping_id DESC);

-- Facility coordinates (nullable: a facility without them simply gets no GPS ETA).
ALTER TABLE facilities ADD COLUMN lat REAL;
ALTER TABLE facilities ADD COLUMN lon REAL;
-- Approximate city centres; refine to the exact dock gate when surveyed.
UPDATE facilities SET lat = 26.9124, lon = 75.7873 WHERE facility_id = 'FAC-JAI-01';
UPDATE facilities SET lat = 28.4595, lon = 77.0266 WHERE facility_id = 'FAC-GGN-01';
