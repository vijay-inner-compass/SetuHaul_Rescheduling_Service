-- Per-(user, IP) password lockout table so a single stranger IP cannot lock an admin out globally.
-- Rollback: DROP TABLE IF EXISTS auth_ip_lockouts; DELETE FROM schema_migrations WHERE version='024_auth_ip_lockouts.sql';
CREATE TABLE auth_ip_lockouts (
    user_id TEXT NOT NULL REFERENCES users(user_id),
    ip TEXT NOT NULL,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    PRIMARY KEY (user_id, ip)
);
