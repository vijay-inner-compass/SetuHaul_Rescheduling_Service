-- Support deleting web users (e.g. warehouse supervisors) while preserving foreign keys on operational history.
-- Rollback: SQLite <3.35 rebuilds users without deleted_at; on SQLite 3.35+: ALTER TABLE users DROP COLUMN deleted_at; DELETE FROM schema_migrations WHERE filename='027_user_deletions.sql';
ALTER TABLE users ADD COLUMN deleted_at TEXT;
