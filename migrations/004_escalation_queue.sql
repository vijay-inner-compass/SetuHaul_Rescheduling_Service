-- Phase 9: coordinator-owned escalation queue with an immutable audit trail.

CREATE TABLE escalation_cases (
    case_id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OPEN','CLAIMED','RESOLVED','CANCELLED')),
    reason_code TEXT NOT NULL,
    driver_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    shipment_id TEXT,
    summary TEXT NOT NULL DEFAULT '',
    context_json TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT 'CONVERSATION',
    created_at TEXT NOT NULL,
    sla_due_at TEXT,               -- NULL unless operations approved an SLA
    claimed_by TEXT,
    claimed_at TEXT,
    resolved_at TEXT,
    resolution_code TEXT,
    resolution_note TEXT,
    reopened_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (driver_id) REFERENCES drivers(driver_id),
    FOREIGN KEY (thread_id) REFERENCES chat_threads(thread_id),
    CHECK (status <> 'CLAIMED' OR claimed_by IS NOT NULL),
    CHECK (status <> 'RESOLVED' OR (resolved_at IS NOT NULL AND resolution_code IS NOT NULL))
);

-- One active case per (driver, thread, shipment, reason): duplicate hand-offs fold into it.
CREATE UNIQUE INDEX ux_active_case_per_key ON escalation_cases(dedupe_key) WHERE status IN ('OPEN','CLAIMED');
CREATE INDEX ix_cases_status_created ON escalation_cases(status, created_at);
CREATE INDEX ix_cases_driver ON escalation_cases(driver_id, created_at);

CREATE TABLE escalation_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('SYSTEM','DRIVER','COORDINATOR')),
    actor_id TEXT,
    from_status TEXT,
    to_status TEXT,
    note TEXT,
    occurred_at TEXT NOT NULL,
    FOREIGN KEY (case_id) REFERENCES escalation_cases(case_id)
);
CREATE INDEX ix_case_events_case ON escalation_events(case_id, event_id);

CREATE TRIGGER escalation_events_no_update BEFORE UPDATE ON escalation_events
BEGIN SELECT RAISE(ABORT, 'escalation_events is immutable'); END;
CREATE TRIGGER escalation_events_no_delete BEFORE DELETE ON escalation_events
BEGIN SELECT RAISE(ABORT, 'escalation_events is immutable'); END;
