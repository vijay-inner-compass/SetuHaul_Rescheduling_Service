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
