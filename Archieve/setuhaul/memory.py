"""History keyed by (driver_id, thread_id), never by token (S4). Booking STATE is not here: it lives in appointments."""
import config

def save_message(c, driver_id, thread_id, role, content):
    c.execute("INSERT INTO messages(driver_id,thread_id,role,content) VALUES(?,?,?,?)", (driver_id, thread_id, role, content))

def load_history(c, driver_id, thread_id):
    rows = c.execute("SELECT role,content FROM messages WHERE driver_id=? AND thread_id=? ORDER BY message_id DESC LIMIT ?",
                     (driver_id, thread_id, config.HISTORY_LIMIT)).fetchall()
    return [dict(role=r["role"], content=r["content"]) for r in reversed(rows)]   # oldest first
