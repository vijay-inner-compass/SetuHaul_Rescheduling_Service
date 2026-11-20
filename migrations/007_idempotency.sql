-- Phase 6: exact-retry semantics. Scope is (driver, thread, message, action); the fingerprint is a
-- canonical hash of the command parameters, so the same key with different parameters is detected.

-- Remembers the answer to each state-changing request, so a retry gets the same answer without acting twice.
CREATE TABLE idempotency_keys (
    driver_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    -- The inbound message id.
    message_id TEXT NOT NULL,
    -- Which action (confirm or cancel).
    action TEXT NOT NULL,
    -- Hash of the request: the same key with different content is detected.
    fingerprint TEXT NOT NULL,
    -- The stored answer.
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    -- The scope of one request.
    PRIMARY KEY (driver_id, thread_id, message_id, action),
    FOREIGN KEY (driver_id) REFERENCES drivers(driver_id),
    FOREIGN KEY (thread_id) REFERENCES chat_threads(thread_id)
);
