"""All SQL lives here (S5: split by reason to change). One connection per caller/thread."""
import sqlite3
from pathlib import Path
import config

def connect(path=None) -> sqlite3.Connection:
    c = sqlite3.connect(path or config.DB_PATH, timeout=30, isolation_level=None)  # manual txns
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def init(path=None):
    c = connect(path)
    c.executescript(Path(__file__).with_name("schema.sql").read_text())
    return c

class Tx:
    """BEGIN IMMEDIATE: take the write lock up front so check-then-write cannot interleave (S7).
    Note (S13 verify list): IMMEDIATE can fail with 'busy'; timeout=30 makes writers wait."""
    def __init__(self, conn): self.c = conn
    def __enter__(self):
        self.c.execute("BEGIN IMMEDIATE"); return self.c
    def __exit__(self, et, ev, tb):
        self.c.execute("ROLLBACK" if et else "COMMIT"); return False

def get_shipment(c, shipment_id):
    return c.execute("SELECT * FROM shipments WHERE shipment_id=?", (shipment_id,)).fetchone()

def get_owned_shipment(c, driver_id, shipment_id):
    """Ownership check (S7): is it THIS driver's shipment?"""
    row = get_shipment(c, shipment_id)
    return row if row and row["driver_id"] == driver_id else None

def driver_by_token(c, token):
    return c.execute("SELECT * FROM drivers WHERE token=?", (token,)).fetchone()
