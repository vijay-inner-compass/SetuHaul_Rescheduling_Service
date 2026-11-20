-- Phase 9: coordinator-owned escalation queue with an immutable audit trail.

-- The hand-off queue: one row per case for operations.
CREATE TABLE escalation_cases (
    case_id TEXT PRIMARY KEY,
    -- Hash of driver, thread, shipment and reason: repeats fold into one case.
    dedupe_key TEXT NOT NULL,
    -- OPEN, CLAIMED, RESOLVED or CANCELLED.
    status TEXT NOT NULL CHECK (status IN ('OPEN','CLAIMED','RESOLVED','CANCELLED')),
    -- Why the case exists.
    reason_code TEXT NOT NULL,
    driver_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    shipment_id TEXT,
    summary TEXT NOT NULL DEFAULT '',
    -- What a coordinator needs: language, recent messages, tools used.
    context_json TEXT NOT NULL DEFAULT '{}',
    -- Where it came from (conversation or fallback file).
    source TEXT NOT NULL DEFAULT 'CONVERSATION',
    created_at TEXT NOT NULL,
    sla_due_at TEXT,               -- NULL unless operations approved an SLA
    claimed_by TEXT,
    claimed_at TEXT,
    resolved_at TEXT,
    resolution_code TEXT,
    resolution_note TEXT,
    -- How many times it was reopened.
    reopened_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (driver_id) REFERENCES drivers(driver_id),
    FOREIGN KEY (thread_id) REFERENCES chat_threads(thread_id),
    -- A claimed case must have an owner.
    CHECK (status <> 'CLAIMED' OR claimed_by IS NOT NULL),
    -- A resolved case must say when and how.
    CHECK (status <> 'RESOLVED' OR (resolved_at IS NOT NULL AND resolution_code IS NOT NULL))
);

-- One active case per (driver, thread, shipment, reason): duplicate hand-offs fold into it.
-- Only one active case per key, enforced by the database.
CREATE UNIQUE INDEX ux_active_case_per_key ON escalation_cases(dedupe_key) WHERE status IN ('OPEN','CLAIMED');
-- Fast queue listing, oldest first.
CREATE INDEX ix_cases_status_created ON escalation_cases(status, created_at);
-- Fast lookup of a driver's cases.
CREATE INDEX ix_cases_driver ON escalation_cases(driver_id, created_at);

-- The append-only history of each case.
CREATE TABLE escalation_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    -- System, driver or coordinator.
    actor_type TEXT NOT NULL CHECK (actor_type IN ('SYSTEM','DRIVER','COORDINATOR')),
    actor_id TEXT,
    from_status TEXT,
    to_status TEXT,
    note TEXT,
    occurred_at TEXT NOT NULL,
    FOREIGN KEY (case_id) REFERENCES escalation_cases(case_id)
);
-- Fast history lookup per case.
CREATE INDEX ix_case_events_case ON escalation_events(case_id, event_id);

-- Case history is immutable too.
CREATE TRIGGER escalation_events_no_update BEFORE UPDATE ON escalation_events
BEGIN SELECT RAISE(ABORT, 'escalation_events is immutable'); END;
CREATE TRIGGER escalation_events_no_delete BEFORE DELETE ON escalation_events
BEGIN SELECT RAISE(ABORT, 'escalation_events is immutable'); END;
