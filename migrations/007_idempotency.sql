-- Phase 6: exact-retry semantics. Scope is (driver, thread, message, action); the fingerprint is a
-- canonical hash of the command parameters, so the same key with different parameters is detected.

CREATE TABLE idempotency_keys (
    driver_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    action TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (driver_id, thread_id, message_id, action),
    FOREIGN KEY (driver_id) REFERENCES drivers(driver_id),
    FOREIGN KEY (thread_id) REFERENCES chat_threads(thread_id)
);
