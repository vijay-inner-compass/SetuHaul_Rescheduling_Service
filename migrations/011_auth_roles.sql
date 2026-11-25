-- Authentication, roles, driver onboarding, projects and the append-only audit log (extension tables only).

-- Web users exist only by invite; drivers are never users. Email matches case-insensitively.
CREATE TABLE users (
    user_id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    role TEXT NOT NULL CHECK (role IN ('ADMIN','WAREHOUSE_AUTHORITY')),
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','DISABLED')),
    display_name TEXT,
    invited_by TEXT REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    last_login_at TEXT
);

-- Facilities a warehouse authority manages.
CREATE TABLE user_facilities (
    user_id TEXT NOT NULL REFERENCES users(user_id),
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    PRIMARY KEY (user_id, facility_id)
);

-- Cookie sessions; session_id is sha256(token), never the token itself.
CREATE TABLE auth_sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    auth_method TEXT NOT NULL CHECK (auth_method IN ('GOOGLE','LOCAL_ADMIN'))
);
-- Revoking all sessions of a disabled user is a lookup by user.
CREATE INDEX ix_auth_sessions_user ON auth_sessions(user_id);

-- Optional break-glass admin password (scrypt); present only if an admin sets one.
CREATE TABLE admin_credentials (
    user_id TEXT PRIMARY KEY REFERENCES users(user_id),
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT
);

-- Onboarding data for drivers added through the app; the supplied drivers table is insert-only.
CREATE TABLE driver_profiles (
    driver_id TEXT PRIMARY KEY REFERENCES drivers(driver_id),
    licence_expiry TEXT NOT NULL,
    vehicle_id TEXT REFERENCES vehicles(vehicle_id),
    approval_status TEXT NOT NULL DEFAULT 'PENDING_APPROVAL'
        CHECK (approval_status IN ('PENDING_APPROVAL','APPROVED','REJECTED')),
    approved_by TEXT REFERENCES users(user_id),
    approved_at TEXT,
    created_by_user_id TEXT REFERENCES users(user_id)
);

-- A driver's Telegram account, bound by shared contact; one account per driver.
CREATE TABLE telegram_identities (
    telegram_user_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    driver_id TEXT NOT NULL UNIQUE REFERENCES drivers(driver_id),
    bound_at TEXT NOT NULL,
    revoked_at TEXT
);

-- Contract projects run by a warehouse authority at one facility.
CREATE TABLE projects (
    project_id TEXT PRIMARY KEY,
    customer_name TEXT NOT NULL,
    contract_ref TEXT NOT NULL UNIQUE,
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','CLOSED')),
    starts_on TEXT,
    ends_on TEXT,
    created_by TEXT NOT NULL REFERENCES users(user_id)
);

-- Drivers allocated to projects; a driver may be on several projects.
CREATE TABLE project_drivers (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    driver_id TEXT NOT NULL REFERENCES drivers(driver_id),
    allocated_by TEXT NOT NULL REFERENCES users(user_id),
    allocated_at TEXT NOT NULL,
    released_at TEXT,
    PRIMARY KEY (project_id, driver_id)
);

-- Who did what, when, with what result; never holds passwords, tokens or codes.
CREATE TABLE audit_log (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor_user_id TEXT,
    actor_email TEXT,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    outcome TEXT NOT NULL,
    ip TEXT,
    detail_json TEXT
);
-- The audit screen filters by action, actor and time.
CREATE INDEX ix_audit_action_ts ON audit_log(action, ts);
CREATE INDEX ix_audit_actor ON audit_log(actor_email, ts);

-- Append-only: reject any UPDATE.
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;
-- Append-only: reject any DELETE.
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;
