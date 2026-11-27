-- Allow retention scrubbing of audit_log.ip (to NULL only) while keeping every other column strictly append-only.
-- Rollback:
--   DROP TRIGGER IF EXISTS audit_log_no_update;
--   CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
--   DELETE FROM schema_migrations WHERE version='025_audit_ip_retention.sql';
DROP TRIGGER IF EXISTS audit_log_no_update;
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
WHEN NEW.audit_id IS NOT OLD.audit_id
  OR NEW.ts IS NOT OLD.ts
  OR NEW.actor_user_id IS NOT OLD.actor_user_id
  OR NEW.actor_email IS NOT OLD.actor_email
  OR NEW.action IS NOT OLD.action
  OR NEW.target_type IS NOT OLD.target_type
  OR NEW.target_id IS NOT OLD.target_id
  OR NEW.outcome IS NOT OLD.outcome
  OR NEW.detail_json IS NOT OLD.detail_json
  OR NEW.ip IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;
