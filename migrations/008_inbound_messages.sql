-- Phase 11: idempotent handling of provider redelivery. Claimed before the model is invoked.
CREATE TABLE inbound_messages (
    driver_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PROCESSING','DONE')),
    reply_json TEXT,
    claimed_epoch INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (driver_id, message_id),
    FOREIGN KEY (driver_id) REFERENCES drivers(driver_id)
);
