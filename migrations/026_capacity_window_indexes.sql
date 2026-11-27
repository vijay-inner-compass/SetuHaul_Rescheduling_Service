-- Indexes to keep capacity window reads and message history reads O(window) instead of O(history) (F08).
-- Rollback:
--   DROP INDEX IF EXISTS ix_slots_facility_end_start;
--   DROP INDEX IF EXISTS ix_labour_facility_end_start;
--   DROP INDEX IF EXISTS ix_dock_events_dock_start;
--   DROP INDEX IF EXISTS ix_chat_messages_thread_ts;
--   DROP INDEX IF EXISTS ix_chat_message_loads_facility;
--   DELETE FROM schema_migrations WHERE version='026_capacity_window_indexes.sql';
CREATE INDEX IF NOT EXISTS ix_slots_facility_end_start ON appointment_slots(facility_id, slot_end_ts, slot_start_ts);
CREATE INDEX IF NOT EXISTS ix_labour_facility_end_start ON labour_windows(facility_id, window_end, window_start);
CREATE INDEX IF NOT EXISTS ix_dock_events_dock_start ON dock_status_events(dock_id, event_start_ts);
CREATE INDEX IF NOT EXISTS ix_chat_messages_thread_ts ON chat_messages(thread_id, message_ts DESC);
CREATE INDEX IF NOT EXISTS ix_chat_message_loads_facility ON chat_message_loads(facility_id, chat_message_id);
