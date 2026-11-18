-- Synthetic stand-in for the supplied 18-table snapshot (swap in an adapter in database.py).
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS facilities (facility_id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS docks (
  dock_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL REFERENCES facilities,
  accepts_reefer INTEGER NOT NULL DEFAULT 0, accepts_heavy INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS slots (
  slot_id TEXT PRIMARY KEY, dock_id TEXT NOT NULL REFERENCES docks,
  start_ts TEXT NOT NULL, end_ts TEXT NOT NULL, blocked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS drivers (
  driver_id TEXT PRIMARY KEY, name TEXT NOT NULL, token TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS shipments (
  shipment_id TEXT PRIMARY KEY, driver_id TEXT NOT NULL REFERENCES drivers,
  facility_id TEXT NOT NULL REFERENCES facilities,
  vehicle_type TEXT NOT NULL CHECK (vehicle_type IN ('standard','reefer','heavy')),
  priority TEXT NOT NULL DEFAULT 'standard' CHECK (priority IN ('standard','high')),
  planned_eta TEXT NOT NULL, current_eta TEXT NOT NULL,
  eta_source TEXT NOT NULL DEFAULT 'plan', eta_confidence TEXT NOT NULL DEFAULT 'planned',
  regulated INTEGER NOT NULL DEFAULT 0);
-- hold -> confirm lifecycle. Showing an option writes NOTHING.
CREATE TABLE IF NOT EXISTS appointments (
  appt_id INTEGER PRIMARY KEY AUTOINCREMENT,
  shipment_id TEXT NOT NULL REFERENCES shipments, slot_id TEXT NOT NULL REFERENCES slots,
  status TEXT NOT NULL CHECK (status IN ('held','confirmed','released','expired')),
  hold_expires_at TEXT, thread_id TEXT, created_at TEXT NOT NULL);
-- Invariants enforced by the database, not by a prompt:
CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_slot ON appointments(slot_id) WHERE status IN ('held','confirmed');
CREATE UNIQUE INDEX IF NOT EXISTS one_hold_per_shipment ON appointments(shipment_id) WHERE status='held';
CREATE UNIQUE INDEX IF NOT EXISTS one_confirmed_per_shipment ON appointments(shipment_id) WHERE status='confirmed';
CREATE TABLE IF NOT EXISTS idempotency (key TEXT PRIMARY KEY, result_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
  message_id INTEGER PRIMARY KEY AUTOINCREMENT, driver_id TEXT NOT NULL, thread_id TEXT NOT NULL,
  role TEXT NOT NULL, content TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_order ON messages(driver_id, thread_id, message_id);
CREATE TABLE IF NOT EXISTS escalations (
  escalation_id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT, shipment_id TEXT,
  reason TEXT NOT NULL, packet_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL);
