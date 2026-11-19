-- Phase 3: driver identity, server-created sessions and durable structured conversation state.
-- Extension tables only; no supplied table is altered.

CREATE TABLE channel_identities (
    phone_e164 TEXT PRIMARY KEY,
    driver_id TEXT NOT NULL UNIQUE,
    provisioned_at TEXT NOT NULL,
    FOREIGN KEY (driver_id) REFERENCES drivers(driver_id)
);

-- session_id stores sha256(token); the raw token is only ever held by the client.
CREATE TABLE driver_sessions (
    session_id TEXT PRIMARY KEY,
    driver_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    FOREIGN KEY (driver_id) REFERENCES drivers(driver_id)
);

CREATE TABLE conversation_state (
    driver_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (driver_id, thread_id),
    FOREIGN KEY (driver_id) REFERENCES drivers(driver_id),
    FOREIGN KEY (thread_id) REFERENCES chat_threads(thread_id)
);
