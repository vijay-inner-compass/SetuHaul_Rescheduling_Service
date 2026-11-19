-- Phase 4: durable record of exactly what each driver was shown. Extension tables only.

CREATE TABLE option_sets (
    option_set_id TEXT PRIMARY KEY,
    driver_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    shipment_id TEXT NOT NULL,
    facility_id TEXT NOT NULL,
    shown_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    context_json TEXT NOT NULL,
    FOREIGN KEY (driver_id) REFERENCES drivers(driver_id),
    FOREIGN KEY (thread_id) REFERENCES chat_threads(thread_id),
    FOREIGN KEY (shipment_id) REFERENCES shipments(shipment_id)
);

CREATE TABLE option_set_items (
    option_set_id TEXT NOT NULL,
    displayed_rank INTEGER NOT NULL CHECK (displayed_rank > 0),
    slot_id TEXT NOT NULL,
    slot_version TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    feasibility_hash TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    PRIMARY KEY (option_set_id, displayed_rank),
    UNIQUE (option_set_id, slot_id),
    FOREIGN KEY (option_set_id) REFERENCES option_sets(option_set_id) ON DELETE CASCADE,
    FOREIGN KEY (slot_id) REFERENCES appointment_slots(slot_id)
);

CREATE INDEX ix_option_sets_scope ON option_sets(driver_id, thread_id, shipment_id, shown_at DESC);
CREATE INDEX ix_option_sets_shown_at ON option_sets(shown_at);
