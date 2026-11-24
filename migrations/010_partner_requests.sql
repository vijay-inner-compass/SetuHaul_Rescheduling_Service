-- Partner-warehouse slot requests: SetuHaul asks a partner by email and books only after their approval reply.
-- A partner facility is one whose docks SetuHaul does not control.
ALTER TABLE facilities ADD COLUMN is_partner INTEGER NOT NULL DEFAULT 0 CHECK (is_partner IN (0,1));

CREATE TABLE partner_requests (
    -- Short reference carried in the email subject, e.g. PR-1A2B3C4D.
    request_ref TEXT PRIMARY KEY,
    shipment_id TEXT NOT NULL REFERENCES shipments(shipment_id),
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    -- The contact the request was emailed to.
    contact_id TEXT NOT NULL REFERENCES facility_contacts(contact_id),
    -- The window asked for (ISO-8601 with offset).
    proposed_start TEXT NOT NULL,
    proposed_end TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING_PARTNER','APPROVED','DECLINED','EXPIRED','FAILED')),
    -- Delivery of the request email: PENDING until sent, FAILED after repeated errors.
    email_status TEXT NOT NULL DEFAULT 'PENDING' CHECK (email_status IN ('PENDING','SENT','FAILED')),
    email_attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    -- The Message-ID we sent, so a reply can be matched by In-Reply-To as well as by subject.
    message_id TEXT,
    -- Set when approved: the slot and appointment created.
    slot_id TEXT,
    appointment_id TEXT,
    reply_message_id TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT
);
-- At most one open request per shipment.
CREATE UNIQUE INDEX ux_partner_open_per_shipment ON partner_requests(shipment_id) WHERE status = 'PENDING_PARTNER';
CREATE INDEX ix_partner_email_status ON partner_requests(email_status, created_at);

-- Every reply email seen, once: the primary key makes re-polling the same mail a no-op.
CREATE TABLE partner_inbound (
    message_id TEXT PRIMARY KEY,
    request_ref TEXT,
    from_addr TEXT NOT NULL,
    outcome TEXT NOT NULL,
    received_at TEXT NOT NULL
);
