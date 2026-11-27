-- A notice no longer needs an appointment event: a case that names no load, or a load that never had an appointment,
-- has nothing to hang one on, and its driver was never told. shipment_id keeps such a notice in its facility's scope.
-- SQLite cannot relax a column, so the table is rebuilt; every row and its id are kept.

CREATE TABLE notification_outbox_new (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    recipient_type TEXT NOT NULL CHECK (recipient_type IN ('DRIVER','WAREHOUSE_PLANNER','CONSIGNEE','OPERATIONS')),
    recipient_ref TEXT NOT NULL,
    -- The history event that caused the notice, when there is one.
    event_id INTEGER,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','SENT','FAILED','PERMANENT_FAILURE')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    -- The load a notice without an event is about, if any.
    shipment_id TEXT,
    FOREIGN KEY (event_id) REFERENCES appointment_events(event_id),
    FOREIGN KEY (shipment_id) REFERENCES shipments(shipment_id)
);

INSERT INTO notification_outbox_new (outbox_id, dedupe_key, recipient_type, recipient_ref, event_id, payload_json, status,
                                     attempts, last_error, created_at, delivered_at)
SELECT outbox_id, dedupe_key, recipient_type, recipient_ref, event_id, payload_json, status, attempts, last_error,
       created_at, delivered_at
FROM notification_outbox;

DROP TABLE notification_outbox;
ALTER TABLE notification_outbox_new RENAME TO notification_outbox;
CREATE INDEX ix_outbox_status ON notification_outbox(status, outbox_id);
