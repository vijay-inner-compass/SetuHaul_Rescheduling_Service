"""Phase 9 evidence: open -> claimed -> resolved replay, competing claims, driver wording, coordinator CLI."""

from __future__ import annotations

import io
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tests.conv_helpers import Chat, say_text, tc
from tests.helpers import at, seed_everything_known

from setuhaul.clock import FakeClock
from setuhaul.coordinator import main as cli
from setuhaul.db import make_working_db, open_connection
from setuhaul.escalation import (
    SqlEscalationSink,
    cancel_case_by_driver,
    case_events,
    claim_case,
    get_driver_case_status,
    resolve_case,
)
from setuhaul.integrity import check_invariants

d = Path(tempfile.mkdtemp())
path = make_working_db(d / "demo.sqlite3")
db = open_connection(path=path)
seed_everything_known(db)
clock = FakeClock(at("09:35"))
chat = Chat(db, clock, d, driver="DRV014", thread="THR004")
chat.sink = SqlEscalationSink(db, clock)  # type: ignore[assignment]

print("== 1. A real conversation hands off (SHP1014: 21,000 kg, 75 min unload) ==")
r = chat.say("SHP1014 is 70 minutes late", [tc("search_slot_options", shipment_id="SHP1014", report={"kind": "late_by", "minutes": 70}), say_text("x")])
print("  DRIVER :", "SHP1014 is 70 minutes late")
print("  SERVICE:", r.text, f"[{r.status} / {r.escalation_reason}]")
cid = r.case_id


def run(*args: str) -> str:
    buf = io.StringIO()
    rc = cli(["--db", str(path), *args], clock=clock, out=buf)
    return f"(exit {rc}) " + buf.getvalue().rstrip()


print("\n== 2. Coordinator queue ==")
print(run("list"))

print("\n== 3. Competing claims ==")
clock.advance(timedelta(minutes=3))
print("  asha:", run("claim", cid, "--as", "asha"))
print("  ben :", run("claim", cid, "--as", "ben"))
print("  ben tries to resolve asha's case:", run("resolve", cid, "--as", "ben", "--code", "ADVISED_DRIVER", "--note", "x"))
print("  driver status now:", get_driver_case_status(db, "DRV014", "THR004").text)  # type: ignore[union-attr]

print("\n== 4. Resolution and what the driver sees in the chat ==")
clock.advance(timedelta(minutes=10))
print("  asha:", run("resolve", cid, "--as", "asha", "--code", "ASSIGNED_SLOT", "--note", "We booked dock D6 at 14:00 for you."))
for row in db.execute("select sender_type, message_text from chat_messages where thread_id='THR004' and sender_type='OPERATIONS' order by rowid"):
    print(f"  [{row[0]}] {row[1]}")

print("\n== 5. Case view for the coordinator ==")
print(run("show", cid))

print("\n== 6. Driver wording by language (no response time promised) ==")
for lang in ("en", "hi", "hinglish"):
    c2 = SqlEscalationSink(db, clock)
    from setuhaul.escalation import HandoffRequest

    cc = c2.record(HandoffRequest(reason_code="SAFETY", driver_id="DRV006", thread_id="THR001", shipment_id=None,
                                  summary=lang, context={"language": lang}, created_at=clock.now()))
    print(f"  {lang:<9}", get_driver_case_status(db, "DRV006", "THR001").text)  # type: ignore[union-attr]
    cancel_case_by_driver(db, cc, "DRV006", clock)

print("\n== 7. Immutable audit trail and invariants ==")
print("  events:", [(e["event_type"], e["actor_id"]) for e in case_events(db, cid)])
print("  check_invariants ->", check_invariants(db))
print("  unused names kept quiet:", claim_case.__name__, resolve_case.__name__)
