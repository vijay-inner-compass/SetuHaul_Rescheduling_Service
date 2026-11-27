-- Adds the SECURITY role (gate guards). SQLite cannot widen a CHECK in place, so the table is rebuilt with the same columns.
CREATE TABLE users_new (
    user_id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    role TEXT NOT NULL CHECK (role IN ('ADMIN','WAREHOUSE_AUTHORITY','SECURITY')),
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','DISABLED')),
    display_name TEXT,
    invited_by TEXT REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    last_login_at TEXT
);
INSERT INTO users_new(user_id, email, role, status, display_name, invited_by, created_at, last_login_at)
    SELECT user_id, email, role, status, display_name, invited_by, created_at, last_login_at FROM users;
DROP TABLE users;
ALTER TABLE users_new RENAME TO users;
