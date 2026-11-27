-- Which load each chat message was about, and that load's destination when it was written. A warehouse authority sees the
-- messages recorded for their facilities. Extension table only: no supplied row or table is changed.
CREATE TABLE chat_message_loads (
    chat_message_id TEXT PRIMARY KEY,
    shipment_id TEXT NOT NULL,
    facility_id TEXT NOT NULL,
    FOREIGN KEY (chat_message_id) REFERENCES chat_messages(chat_message_id),
    FOREIGN KEY (shipment_id) REFERENCES shipments(shipment_id),
    FOREIGN KEY (facility_id) REFERENCES facilities(facility_id)
);
CREATE INDEX ix_chat_message_loads_facility ON chat_message_loads(facility_id);

-- Existing messages whose thread already names a shipment. Other old messages stay visible to administrators only.
INSERT INTO chat_message_loads(chat_message_id, shipment_id, facility_id)
SELECT m.chat_message_id, s.shipment_id, s.destination_facility_id
FROM chat_messages m JOIN chat_threads t ON t.thread_id = m.thread_id JOIN shipments s ON s.shipment_id = t.shipment_id;
