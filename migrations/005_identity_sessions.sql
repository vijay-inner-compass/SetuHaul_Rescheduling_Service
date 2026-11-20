-- Phase 3: driver identity, server-created sessions and durable structured conversation state.
-- Extension tables only; no supplied table is altered.

-- Maps a verified phone number to exactly one driver.
CREATE TABLE channel_identities (
    -- The number in international format.
    phone_e164 TEXT PRIMARY KEY,
    -- One number per driver.
    driver_id TEXT NOT NULL UNIQUE,
    provisioned_at TEXT NOT NULL,
    FOREIGN KEY (driver_id) REFERENCES drivers(driver_id)
);

-- session_id stores sha256(token); the raw token is only ever held by the client.
-- Server-issued sessions.
CREATE TABLE driver_sessions (
    session_id TEXT PRIMARY KEY,
    driver_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    -- Sessions expire (8 hours by default).
    expires_at TEXT NOT NULL,
    -- Set when a session is cancelled early.
    revoked_at TEXT,
    FOREIGN KEY (driver_id) REFERENCES drivers(driver_id)
);

-- The durable structured state of a conversation (selected shipment, latest menu, hold, language).
CREATE TABLE conversation_state (
    driver_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    -- The state as JSON.
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- One state per driver and thread.
    PRIMARY KEY (driver_id, thread_id),
    FOREIGN KEY (driver_id) REFERENCES drivers(driver_id),
    FOREIGN KEY (thread_id) REFERENCES chat_threads(thread_id)
);
