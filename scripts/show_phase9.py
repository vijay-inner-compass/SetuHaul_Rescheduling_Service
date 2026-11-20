"""Phase 9 evidence: open -> claimed -> resolved replay, competing claims, driver wording, coordinator CLI."""

# Lazy annotations.
from __future__ import annotations

# A text buffer to capture CLI output.
import io

# sys.path setup.
import sys

# Scratch database folder.
import tempfile

# Move the clock.
from datetime import timedelta

# Path arithmetic.
from pathlib import Path

# Make tests helpers importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Make src/ importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Scripted chat helpers.
from tests.conv_helpers import Chat, say_text, tc

# Time helper and standard evidence.
from tests.helpers import at, seed_everything_known

# Fixed clock.
from setuhaul.clock import FakeClock

# The coordinator command-line tool.
from setuhaul.coordinator import main as cli

# Database helpers.
from setuhaul.db import make_working_db, open_connection

# Queue operations.
from setuhaul.escalation import (
    SqlEscalationSink,
    cancel_case_by_driver,
    case_events,
    claim_case,
    get_driver_case_status,
    resolve_case,
)

# Invariants.
from setuhaul.integrity import check_invariants

# Scratch folder.
d = Path(tempfile.mkdtemp())
# A fresh database copy.
path = make_working_db(d / "demo.sqlite3")
# Open it.
db = open_connection(path=path)
# Standard test evidence.
seed_everything_known(db)
# 09:35 on the snapshot day.
clock = FakeClock(at("09:35"))
# A scripted chat for driver DRV014 (a heavy load that fits nowhere).
chat = Chat(db, clock, d, driver="DRV014", thread="THR004")
# Hand-offs go to the real SQL queue.
chat.sink = SqlEscalationSink(db, clock)  # type: ignore[assignment]

# Part 1: a real conversation hands off.
print("== 1. A real conversation hands off (SHP1014: 21,000 kg, 75 min unload) ==")
# The model searches; nothing fits, so the system hands off.
r = chat.say("SHP1014 is 70 minutes late", [tc("search_slot_options", shipment_id="SHP1014", report={"kind": "late_by", "minutes": 70}), say_text("x")])
# What the driver wrote.
print("  DRIVER :", "SHP1014 is 70 minutes late")
# What they were told, with the case reference.
print("  SERVICE:", r.text, f"[{r.status} / {r.escalation_reason}]")
# The case id.
cid = r.case_id


# Run the coordinator CLI and capture its output.
def run(*args: str) -> str:
    # An in-memory output.
    buf = io.StringIO()
    # Run the CLI against this database.
    rc = cli(["--db", str(path), *args], clock=clock, out=buf)
    # Exit code and output.
    return f"(exit {rc}) " + buf.getvalue().rstrip()


# Part 2: the coordinator queue.
print("\n== 2. Coordinator queue ==")
# List open cases.
print(run("list"))

# Part 3: competing claims.
print("\n== 3. Competing claims ==")
# Three minutes pass.
clock.advance(timedelta(minutes=3))
# Asha claims it.
print("  asha:", run("claim", cid, "--as", "asha"))
# Ben's claim is refused: Asha holds it.
print("  ben :", run("claim", cid, "--as", "ben"))
# Ben cannot resolve Asha's case.
print("  ben tries to resolve asha's case:", run("resolve", cid, "--as", "ben", "--code", "ADVISED_DRIVER", "--note", "x"))
# The driver is told a coordinator is handling it.
print("  driver status now:", get_driver_case_status(db, "DRV014", "THR004").text)  # type: ignore[union-attr]

# Part 4: resolution reaches the driver.
print("\n== 4. Resolution and what the driver sees in the chat ==")
# Ten more minutes.
clock.advance(timedelta(minutes=10))
# Asha resolves it with a note.
print("  asha:", run("resolve", cid, "--as", "asha", "--code", "ASSIGNED_SLOT", "--note", "We booked dock D6 at 14:00 for you."))
# The operations messages now in the driver's chat.
for row in db.execute("select sender_type, message_text from chat_messages where thread_id='THR004' and sender_type='OPERATIONS' order by rowid"):
    # Print each.
    print(f"  [{row[0]}] {row[1]}")

# Part 5: the coordinator's full case view.
print("\n== 5. Case view for the coordinator ==")
# Show it.
print(run("show", cid))

# Part 6: driver wording in three languages.
print("\n== 6. Driver wording by language (no response time promised) ==")
# Each language.
for lang in ("en", "hi", "hinglish"):
    # A sink on the same database.
    c2 = SqlEscalationSink(db, clock)
    # The request type.
    from setuhaul.escalation import HandoffRequest

    # Open a case with that language in its context.
    cc = c2.record(HandoffRequest(reason_code="SAFETY", driver_id="DRV006", thread_id="THR001", shipment_id=None,
                                  summary=lang, context={"language": lang}, created_at=clock.now()))
    # What the driver is told; no response time is promised.
    print(f"  {lang:<9}", get_driver_case_status(db, "DRV006", "THR001").text)  # type: ignore[union-attr]
    # Cancel it to keep cases from clashing.
    cancel_case_by_driver(db, cc, "DRV006", clock)

# Part 7: audit trail and invariants.
print("\n== 7. Immutable audit trail and invariants ==")
# The immutable events of the first case.
print("  events:", [(e["event_type"], e["actor_id"]) for e in case_events(db, cid)])
# An empty list means the database is consistent.
print("  check_invariants ->", check_invariants(db))
# Keeps unused imports quiet.
print("  unused names kept quiet:", claim_case.__name__, resolve_case.__name__)
