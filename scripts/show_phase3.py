"""Print the Phase 3 evidence bundle: auth trace, rate-limit response, two-shipment thread, restart."""

# Lazy annotations.
from __future__ import annotations

# SHA-256 for the signature.
import hashlib

# Builds a valid webhook signature.
import hmac

# sys.path setup.
import sys

# Scratch database folder.
import tempfile

# Time steps.
from datetime import timedelta

# Path arithmetic.
from pathlib import Path

# Make src/ importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# In-process fake Redis.
import fakeredis

# Fixed clock.
from setuhaul.clock import FakeClock

# Settings.
from setuhaul.config import load_settings

# Database helpers.
from setuhaul.db import make_working_db, open_connection

# Short alias.
from setuhaul.db import parse_operational_timestamp as ts

# Identity functions being demonstrated.
from setuhaul.identity import (
    AuthError,
    authenticate_session,
    authorize_shipment,
    create_session,
    provision_identities,
    resolve_driver,
    verify_channel_request,
)

# Conversation memory.
from setuhaul.memory import ConversationMemoryStore, HoldRef

# Rate limiting.
from setuhaul.rate_limit import RateLimits, check_rate_limit

# A demo webhook secret.
SECRET = b"demo-secret"
# Fixed clock at 09:35 on the snapshot day.
clock = FakeClock(ts("2026-08-04T09:35:00+05:30"))
# Scratch folder.
tmp = Path(tempfile.mkdtemp())
# A fresh working database.
db = open_connection(path=make_working_db(tmp / "demo.sqlite3"))
# Fake Redis.
redis = fakeredis.FakeRedis(decode_responses=True)


# Run an action and print whether it succeeded or was rejected with a code.
def attempt(label: str, fn):  # type: ignore[no-untyped-def]
    try:
        out = fn()
        print(f"  {label}: OK -> {out}")
    except AuthError as exc:
        print(f"  {label}: REJECTED code={exc.code}")


# Part 1: webhook signature, phone lookup, sessions, ownership.
print("== authentication trace ==")
# A sample webhook body.
body = b'{"from":"+91 90000 10006","text":"truck is stuck in traffic"}'
# Its correct signature.
good = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
# A wrong signature is rejected.
attempt("bad signature", lambda: verify_channel_request(body, "0" * 64, SECRET))
# The right one passes.
attempt("good signature", lambda: verify_channel_request(body, good, SECRET))
# Load the phone-to-driver map.
print(f"  provisioned identities: {provision_identities(db)}")
# An unknown number is refused.
attempt("unknown phone", lambda: resolve_driver(db, "+919999999999"))
# A known number, written differently, resolves to the same driver.
ctx = resolve_driver(db, "+91 90000 10006")
print(f"  resolved: {ctx.model_dump()}")
# Issue a 30-minute session.
session = create_session(db, ctx.driver_id, clock, timedelta(minutes=30))
# What the database stores for it.
stored = db.execute("select session_id from driver_sessions").fetchone()[0]
# The stored id is a hash, not the token.
print(f"  session token len={len(session.token)}; stored id is sha256: {stored[:12]}...")
# Presenting a driver id as a token is refused.
attempt("client-supplied driver id as session", lambda: authenticate_session(db, "DRV006", clock))
# The real token works.
attempt("session auth", lambda: authenticate_session(db, session.token, clock).driver_id)
# Another driver's shipment is refused.
attempt("foreign shipment SHP1012", lambda: authorize_shipment(db, ctx, "SHP1012"))
# 31 minutes later...
clock.advance(timedelta(minutes=31))
# ...the session has expired.
attempt("session after 31 min", lambda: authenticate_session(db, session.token, clock))
# Back to 09:35.
clock.set(ts("2026-08-04T09:35:00+05:30"))

# Part 2: 20 messages a minute.
print("\n== rate limit (20/min, 100/h) ==")
# Send 21 messages.
for i in range(1, 22):
    # Check each.
    r = check_rate_limit(redis, "DRV006", clock, RateLimits(20, 100))
    # Show the first, the last allowed and the first refused.
    if i in (1, 20, 21):
        print(f"  message {i}: allowed={r.allowed} reason={r.reason_code} retry_after={r.retry_after_seconds}s")

# Part 3: one driver, two shipments, one thread.
print("\n== two shipments in one thread (DRV006: SHP1006 and SHP1021) ==")
# The memory store.
store = ConversationMemoryStore(db, redis, clock, load_settings())
# Open a thread.
thread = store.open_thread("DRV006", shipment_id="SHP1006")
# The driver says they are late.
store.append("DRV006", thread, "DRIVER", "I am 60 minutes late")
# The assistant asks which shipment.
store.append("DRV006", thread, "AGENT", "Which shipment, SHP1006 or SHP1021?")
# Load the state.
c = store.load("DRV006", thread)
# Both shipments are active; one is selected.
print(f"  active={c.state.active_shipment_ids} selected={c.state.selected_shipment_id}")
# The driver explicitly switches.
store.select_shipment("DRV006", thread, "SHP1021")
# The selection changed.
print(f"  after explicit switch selected={store.load('DRV006', thread).state.selected_shipment_id}")
# Selecting someone else's shipment is refused...
attempt("select SHP1012", lambda: store.select_shipment("DRV006", thread, "SHP1012"))
# ...and the selection is unchanged.
print(f"  still selected={store.load('DRV006', thread).state.selected_shipment_id}")
# Check ownership data is unchanged.
owners = db.execute("select shipment_id, driver_id from shipments where shipment_id in ('SHP1006','SHP1021')").fetchall()
# Print it.
print(f"  ownership unchanged: {[tuple(o) for o in owners]}")

# Part 4: losing the cache loses nothing.
print("\n== restart / Redis loss reconstruction ==")
# Add 30 messages (more than the 20-turn cap).
for i in range(30):
    store.append("DRV006", thread, "DRIVER" if i % 2 == 0 else "AGENT", f"turn {i}")
# A brand-new worker with an empty cache.
fresh = ConversationMemoryStore(db, fakeredis.FakeRedis(decode_responses=True), clock, load_settings())
# It rebuilds from the database.
r = fresh.load("DRV006", thread)
# Shows it was not from cache, that the window is capped at 20 and the selection survived.
print(f"  new worker + empty cache: from_cache={r.from_cache} turns={len(r.turns)} (cap 20) "
      f"selected={r.state.selected_shipment_id}")
# All messages are still in SQL.
total = db.execute("select count(*) from chat_messages where thread_id=?", (thread,)).fetchone()[0]
# Print the count.
print(f"  durable messages in SQL: {total}")

# Part 5: a remembered hold that has lapsed.
print("\n== return after hold expiry ==")
# An old promise in the history.
store.append("DRV006", thread, "AGENT", "I am holding the 11:00 slot for you.")
# Current state.
s = store.load("DRV006", thread).state
# Record a hold that expires in 3 minutes.
store.save_structured_state("DRV006", thread, s.model_copy(update={"hold": HoldRef(
    appointment_id="APT-H1", expires_at=clock.now() + timedelta(minutes=3))}))
# Five minutes pass.
clock.advance(timedelta(minutes=5))
# The driver returns.
back = store.load("DRV006", thread)
# The hold is gone and an expiry event is pending.
print(f"  events={[e.type for e in back.events]} hold_in_state={back.state.hold}")
# The old promise stays in history but is not treated as live.
print(f"  last stored agent promise still in history (not replayed as live): {back.turns[-1].text!r}")
