-- Phase 11: idempotent handling of provider redelivery. Claimed before the model is invoked.
-- One row per delivered driver message, claimed before the model is called.
CREATE TABLE inbound_messages (
    driver_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    -- Hash of the message text: the same id with different text is a conflict.
    fingerprint TEXT NOT NULL,
    -- PROCESSING while being handled, DONE when answered.
    status TEXT NOT NULL CHECK (status IN ('PROCESSING','DONE')),
    -- The stored reply, returned for a duplicate delivery.
    reply_json TEXT,
    -- When it was claimed, so a stuck claim can be reclaimed after 60 seconds.
    claimed_epoch INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    -- When it finished.
    completed_at TEXT,
    -- A message id is unique per driver.
    PRIMARY KEY (driver_id, message_id),
    FOREIGN KEY (driver_id) REFERENCES drivers(driver_id)
);
