-- Phase 5 (events, outbox, maintenance) ahead of Phase 6 (idempotency lands in 007).
-- Expiry must append HOLD_EXPIRED and queue a driver notice in the same transaction, so the
-- event and outbox tables are needed now. See decision D23.

CREATE TABLE appointment_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id TEXT NOT NULL,
    shipment_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('DRIVER','SYSTEM','OPERATIONS','WAREHOUSE')),
    actor_id TEXT,
    reason TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    occurred_at TEXT NOT NULL,
    FOREIGN KEY (appointment_id) REFERENCES appointments(appointment_id),
    FOREIGN KEY (shipment_id) REFERENCES shipments(shipment_id)
);
CREATE INDEX ix_appointment_events_appt ON appointment_events(appointment_id, event_id);

-- History is immutable.
CREATE TRIGGER appointment_events_no_update BEFORE UPDATE ON appointment_events
BEGIN SELECT RAISE(ABORT, 'appointment_events is immutable'); END;
CREATE TRIGGER appointment_events_no_delete BEFORE DELETE ON appointment_events
BEGIN SELECT RAISE(ABORT, 'appointment_events is immutable'); END;

CREATE TABLE notification_outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    recipient_type TEXT NOT NULL CHECK (recipient_type IN ('DRIVER','WAREHOUSE_PLANNER','CONSIGNEE','OPERATIONS')),
    recipient_ref TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','SENT','FAILED','PERMANENT_FAILURE')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    FOREIGN KEY (event_id) REFERENCES appointment_events(event_id)
);
CREATE INDEX ix_outbox_status ON notification_outbox(status, outbox_id);

CREATE TABLE maintenance_runs (
    job_name TEXT PRIMARY KEY,
    last_run_at TEXT NOT NULL,
    last_run_epoch INTEGER NOT NULL
);
