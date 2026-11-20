-- Phase 5 (events, outbox, maintenance) ahead of Phase 6 (idempotency lands in 007).
-- Expiry must append HOLD_EXPIRED and queue a driver notice in the same transaction, so the
-- event and outbox tables are needed now. See decision D23.

-- The append-only history of every change to an appointment or hold.
CREATE TABLE appointment_events (
    -- Ever-increasing id: defines order.
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id TEXT NOT NULL,
    shipment_id TEXT NOT NULL,
    -- What happened (HOLD_ACQUIRED, HOLD_EXPIRED, RESCHEDULE_CONFIRMED ...).
    event_type TEXT NOT NULL,
    -- Status before.
    from_status TEXT,
    -- Status after.
    to_status TEXT,
    -- Who did it: driver, system, operations or warehouse.
    actor_type TEXT NOT NULL CHECK (actor_type IN ('DRIVER','SYSTEM','OPERATIONS','WAREHOUSE')),
    -- Which one.
    actor_id TEXT,
    -- Why.
    reason TEXT,
    -- Extra facts as JSON.
    details_json TEXT NOT NULL DEFAULT '{}',
    -- When.
    occurred_at TEXT NOT NULL,
    -- Every event points at a real appointment and shipment.
    FOREIGN KEY (appointment_id) REFERENCES appointments(appointment_id),
    FOREIGN KEY (shipment_id) REFERENCES shipments(shipment_id)
);
-- Fast history lookup per appointment.
CREATE INDEX ix_appointment_events_appt ON appointment_events(appointment_id, event_id);

-- History is immutable.
-- These triggers make history immutable: any UPDATE or DELETE is aborted.
CREATE TRIGGER appointment_events_no_update BEFORE UPDATE ON appointment_events
BEGIN SELECT RAISE(ABORT, 'appointment_events is immutable'); END;
CREATE TRIGGER appointment_events_no_delete BEFORE DELETE ON appointment_events
BEGIN SELECT RAISE(ABORT, 'appointment_events is immutable'); END;

-- The transactional outbox: notices are queued in the same transaction as the change, and delivered later.
CREATE TABLE notification_outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- A stable key: queueing the same notice twice is impossible.
    dedupe_key TEXT NOT NULL UNIQUE,
    -- Driver, warehouse planner, consignee or operations.
    recipient_type TEXT NOT NULL CHECK (recipient_type IN ('DRIVER','WAREHOUSE_PLANNER','CONSIGNEE','OPERATIONS')),
    -- Who exactly.
    recipient_ref TEXT NOT NULL,
    -- The history event that caused the notice.
    event_id INTEGER NOT NULL,
    -- The notice content.
    payload_json TEXT NOT NULL,
    -- PENDING, SENT, FAILED (will retry) or PERMANENT_FAILURE.
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','SENT','FAILED','PERMANENT_FAILURE')),
    -- Delivery attempts so far.
    attempts INTEGER NOT NULL DEFAULT 0,
    -- The latest delivery error.
    last_error TEXT,
    created_at TEXT NOT NULL,
    -- When delivery succeeded.
    delivered_at TEXT,
    FOREIGN KEY (event_id) REFERENCES appointment_events(event_id)
);
-- Fast selection of notices still to send.
CREATE INDEX ix_outbox_status ON notification_outbox(status, outbox_id);

-- When each housekeeping job last ran, so it is not repeated too soon.
CREATE TABLE maintenance_runs (
    job_name TEXT PRIMARY KEY,
    last_run_at TEXT NOT NULL,
    last_run_epoch INTEGER NOT NULL
);
