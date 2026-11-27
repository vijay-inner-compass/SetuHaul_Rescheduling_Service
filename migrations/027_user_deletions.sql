-- Track soft-deleted web users (e.g. warehouse supervisors with operational history) without altering the users table.
-- Rollback: DROP TABLE IF EXISTS user_deletions; DELETE FROM schema_migrations WHERE filename = '027_user_deletions.sql';
CREATE TABLE user_deletions (
    user_id TEXT PRIMARY KEY REFERENCES users(user_id),
    deleted_by TEXT NOT NULL REFERENCES users(user_id),
    deleted_at TEXT NOT NULL,
    original_email TEXT NOT NULL
);
