-- Who blocked a dock from the screen, and who reopened a block and when. Extension table only: the event row itself is
-- never changed. A supplied event gets a row only when someone reopens it, so created_by is then empty.
CREATE TABLE dock_blocks (
    dock_event_id TEXT PRIMARY KEY,
    created_by TEXT,
    created_at TEXT NOT NULL,
    ended_by TEXT,
    ended_at TEXT,
    FOREIGN KEY (dock_event_id) REFERENCES dock_status_events(dock_event_id),
    FOREIGN KEY (created_by) REFERENCES users(user_id),
    FOREIGN KEY (ended_by) REFERENCES users(user_id)
);
