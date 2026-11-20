"""Synthetic Jaipur-like day (replace with the supplied snapshot via database.py)."""
from datetime import datetime
import database

DAY = "2026-08-04"
NOW = datetime(2026, 8, 4, 9, 0)

def seed(c, n_drivers=12):
    c.execute("INSERT INTO facilities VALUES('JPR','Jaipur DC')")
    # D1,D2 standard; D3 reefer+heavy; D4 heavy only
    for d, r, h in [("D1", 0, 0), ("D2", 0, 0), ("D3", 1, 1), ("D4", 0, 1)]:
        c.execute("INSERT INTO docks VALUES(?,?,?,?)", (d, "JPR", r, h))
        for hh in range(8, 18):
            blocked = 1 if (d, hh) in {("D1", 10), ("D3", 11)} else 0
            c.execute("INSERT INTO slots VALUES(?,?,?,?,?)", (f"{d}-{hh:02d}", d, f"{DAY}T{hh:02d}:00", f"{DAY}T{hh+1:02d}:00", blocked))
    for i in range(1, n_drivers + 1):
        c.execute("INSERT INTO drivers VALUES(?,?,?)", (f"DRV{i:03d}", f"Driver {i}", f"tok-{i:03d}"))
        c.execute("INSERT INTO shipments VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (f"SHP{1000+i}", f"DRV{i:03d}", "JPR", "standard", "standard", f"{DAY}T09:00", f"{DAY}T09:00", "plan", "planned", 0))

def make_db(path):
    c = database.init(path); seed(c); return c
