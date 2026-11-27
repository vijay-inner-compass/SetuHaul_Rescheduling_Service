-- Email and password sign-in: one-time links, and sessions that record a password sign-in.

-- A link emailed to set a password. Only sha256(token) is stored; used_at is also stamped when a newer link replaces it.
CREATE TABLE account_links (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id),
    purpose TEXT NOT NULL CHECK (purpose IN ('INVITE','RESET')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE INDEX ix_account_links_user ON account_links(user_id);

-- SQLite cannot widen a CHECK in place, so the sessions table is rebuilt with every row kept.
CREATE TABLE auth_sessions_new (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    auth_method TEXT NOT NULL CHECK (auth_method IN ('GOOGLE','LOCAL_ADMIN','PASSWORD'))
);
INSERT INTO auth_sessions_new SELECT session_id, user_id, created_at, expires_at, revoked_at, auth_method FROM auth_sessions;
DROP TABLE auth_sessions;
ALTER TABLE auth_sessions_new RENAME TO auth_sessions;
CREATE INDEX ix_auth_sessions_user ON auth_sessions(user_id);
