-- Settings an admin saved on the Configuration tab. Each overrides the environment variable of the same name.
-- Secrets are stored encrypted (encrypted = 1) with the master key SETUHAUL_SETTINGS_KEY, which is not in this database.
CREATE TABLE app_settings (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    encrypted INTEGER NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
