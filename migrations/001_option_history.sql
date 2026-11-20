-- Phase 4: durable record of exactly what each driver was shown. Extension tables only.

-- One row per menu shown to a driver: who saw it, when, until when, and the full context it was computed in.
CREATE TABLE option_sets (
    -- Unique menu id (OPT-...).
    option_set_id TEXT PRIMARY KEY,
    -- The driver it was shown to.
    driver_id TEXT NOT NULL,
    -- The conversation thread.
    thread_id TEXT NOT NULL,
    -- The shipment it was for.
    shipment_id TEXT NOT NULL,
    -- The facility whose slots were offered.
    facility_id TEXT NOT NULL,
    -- When it was shown.
    shown_at TEXT NOT NULL,
    -- After this the menu can no longer be selected (15 minutes).
    expires_at TEXT NOT NULL,
    -- Fingerprint of the facility rules at the time.
    rules_version TEXT NOT NULL,
    -- The feasibility context as JSON, so the menu can be re-evaluated exactly.
    context_json TEXT NOT NULL,
    -- The menu must belong to a real driver, thread and shipment.
    FOREIGN KEY (driver_id) REFERENCES drivers(driver_id),
    FOREIGN KEY (thread_id) REFERENCES chat_threads(thread_id),
    FOREIGN KEY (shipment_id) REFERENCES shipments(shipment_id)
);

-- One row per line of a menu.
CREATE TABLE option_set_items (
    option_set_id TEXT NOT NULL,
    -- Position shown to the driver (1, 2, 3). Must be positive.
    displayed_rank INTEGER NOT NULL CHECK (displayed_rank > 0),
    -- The slot offered.
    slot_id TEXT NOT NULL,
    -- Slot fingerprint when shown: a later difference means it changed.
    slot_version TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    -- Hash of the decision when shown.
    feasibility_hash TEXT NOT NULL,
    -- The whole decision as JSON.
    decision_json TEXT NOT NULL,
    -- A rank appears once per menu.
    PRIMARY KEY (option_set_id, displayed_rank),
    -- A slot appears once per menu.
    UNIQUE (option_set_id, slot_id),
    -- Deleting a menu deletes its lines; each line points at a real slot.
    FOREIGN KEY (option_set_id) REFERENCES option_sets(option_set_id) ON DELETE CASCADE,
    FOREIGN KEY (slot_id) REFERENCES appointment_slots(slot_id)
);

-- Fast lookup of a driver's latest menu in a thread for a shipment.
CREATE INDEX ix_option_sets_scope ON option_sets(driver_id, thread_id, shipment_id, shown_at DESC);
-- Fast retention purge by age.
CREATE INDEX ix_option_sets_shown_at ON option_sets(shown_at);
