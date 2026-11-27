-- A deleted driver is hidden and inactive, never erased: history points at them. One open deletion per driver.
CREATE TABLE driver_deletions (
    deletion_id INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_id TEXT NOT NULL,
    deleted_by TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    previous_status TEXT NOT NULL,
    restored_by TEXT,
    restored_at TEXT,
    FOREIGN KEY (driver_id) REFERENCES drivers(driver_id),
    FOREIGN KEY (deleted_by) REFERENCES users(user_id),
    FOREIGN KEY (restored_by) REFERENCES users(user_id)
);
CREATE UNIQUE INDEX ux_driver_open_deletion ON driver_deletions(driver_id) WHERE restored_at IS NULL;
