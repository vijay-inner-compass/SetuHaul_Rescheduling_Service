"""Print the Phase 3 evidence bundle: auth trace, rate-limit response, two-shipment thread, restart."""

from __future__ import annotations

import hashlib
import hmac
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import fakeredis

from setuhaul.clock import FakeClock
from setuhaul.config import load_settings
from setuhaul.db import make_working_db, open_connection
from setuhaul.db import parse_operational_timestamp as ts
from setuhaul.identity import (
    AuthError,
    authenticate_session,
    authorize_shipment,
    create_session,
    provision_identities,
    resolve_driver,
    verify_channel_request,
)
from setuhaul.memory import ConversationMemoryStore, HoldRef
from setuhaul.rate_limit import RateLimits, check_rate_limit

SECRET = b"demo-secret"
clock = FakeClock(ts("2026-08-04T09:35:00+05:30"))
tmp = Path(tempfile.mkdtemp())
db = open_connection(path=make_working_db(tmp / "demo.sqlite3"))
redis = fakeredis.FakeRedis(decode_responses=True)


def attempt(label: str, fn):  # type: ignore[no-untyped-def]
    try:
        out = fn()
        print(f"  {label}: OK -> {out}")
    except AuthError as exc:
        print(f"  {label}: REJECTED code={exc.code}")


print("== authentication trace ==")
body = b'{"from":"+91 90000 10006","text":"truck is stuck in traffic"}'
good = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
attempt("bad signature", lambda: verify_channel_request(body, "0" * 64, SECRET))
attempt("good signature", lambda: verify_channel_request(body, good, SECRET))
print(f"  provisioned identities: {provision_identities(db)}")
attempt("unknown phone", lambda: resolve_driver(db, "+919999999999"))
ctx = resolve_driver(db, "+91 90000 10006")
print(f"  resolved: {ctx.model_dump()}")
session = create_session(db, ctx.driver_id, clock, timedelta(minutes=30))
stored = db.execute("select session_id from driver_sessions").fetchone()[0]
print(f"  session token len={len(session.token)}; stored id is sha256: {stored[:12]}...")
attempt("client-supplied driver id as session", lambda: authenticate_session(db, "DRV006", clock))
attempt("session auth", lambda: authenticate_session(db, session.token, clock).driver_id)
attempt("foreign shipment SHP1012", lambda: authorize_shipment(db, ctx, "SHP1012"))
clock.advance(timedelta(minutes=31))
attempt("session after 31 min", lambda: authenticate_session(db, session.token, clock))
clock.set(ts("2026-08-04T09:35:00+05:30"))

print("\n== rate limit (20/min, 100/h) ==")
for i in range(1, 22):
    r = check_rate_limit(redis, "DRV006", clock, RateLimits(20, 100))
    if i in (1, 20, 21):
        print(f"  message {i}: allowed={r.allowed} reason={r.reason_code} retry_after={r.retry_after_seconds}s")

print("\n== two shipments in one thread (DRV006: SHP1006 and SHP1021) ==")
store = ConversationMemoryStore(db, redis, clock, load_settings())
thread = store.open_thread("DRV006", shipment_id="SHP1006")
store.append("DRV006", thread, "DRIVER", "I am 60 minutes late")
store.append("DRV006", thread, "AGENT", "Which shipment, SHP1006 or SHP1021?")
c = store.load("DRV006", thread)
print(f"  active={c.state.active_shipment_ids} selected={c.state.selected_shipment_id}")
store.select_shipment("DRV006", thread, "SHP1021")
print(f"  after explicit switch selected={store.load('DRV006', thread).state.selected_shipment_id}")
attempt("select SHP1012", lambda: store.select_shipment("DRV006", thread, "SHP1012"))
print(f"  still selected={store.load('DRV006', thread).state.selected_shipment_id}")
owners = db.execute("select shipment_id, driver_id from shipments where shipment_id in ('SHP1006','SHP1021')").fetchall()
print(f"  ownership unchanged: {[tuple(o) for o in owners]}")

print("\n== restart / Redis loss reconstruction ==")
for i in range(30):
    store.append("DRV006", thread, "DRIVER" if i % 2 == 0 else "AGENT", f"turn {i}")
fresh = ConversationMemoryStore(db, fakeredis.FakeRedis(decode_responses=True), clock, load_settings())
r = fresh.load("DRV006", thread)
print(f"  new worker + empty cache: from_cache={r.from_cache} turns={len(r.turns)} (cap 20) "
      f"selected={r.state.selected_shipment_id}")
total = db.execute("select count(*) from chat_messages where thread_id=?", (thread,)).fetchone()[0]
print(f"  durable messages in SQL: {total}")

print("\n== return after hold expiry ==")
store.append("DRV006", thread, "AGENT", "I am holding the 11:00 slot for you.")
s = store.load("DRV006", thread).state
store.save_structured_state("DRV006", thread, s.model_copy(update={"hold": HoldRef(
    appointment_id="APT-H1", expires_at=clock.now() + timedelta(minutes=3))}))
clock.advance(timedelta(minutes=5))
back = store.load("DRV006", thread)
print(f"  events={[e.type for e in back.events]} hold_in_state={back.state.hold}")
print(f"  last stored agent promise still in history (not replayed as live): {back.turns[-1].text!r}")
