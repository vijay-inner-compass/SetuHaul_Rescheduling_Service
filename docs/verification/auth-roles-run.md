# Auth and roles: verification run

Run on 2026-11-25, Windows, branch `feat/auth-roles`, code at commit `77f5867` (documentation changes uncommitted at the time).
Google and Telegram were **faked**; no network call and no real credential was used. This is not evidence that the real Google or Telegram
services work, and no browser was driven (screens were requested through the in-process test client only).

The plan asked for a smoke "against `scripts/run_demo.py` with the fake verifier switch". There is no such switch (the demo app has no
Google or Telegram), so the smoke runs against `create_app_from_env`, then swaps the Google verifier and Telegram client for the fakes.

## Quality gates

The shared virtual environment of the main checkout was used, with `PYTHONPATH=src` so the worktree's code is the code under test:
`PY=/c/GitHub/SetuHaul_Rescheduling_Service/.venv/Scripts/python.exe`.

```text
$ PYTHONPATH=src $PY -m ruff check .
All checks passed!                                                   (exit 0)

$ PYTHONPATH=src $PY -m mypy src
Success: no issues found in 67 source files                          (exit 0)

$ PYTHONPATH=src $PY scripts/check_comment_only.py                   # against HEAD, after the code fixes were committed
0 changed file(s) checked, 0 with code changes                       (exit 0)

$ PYTHONPATH=src $PY scripts/check_comment_only.py 6bb5fc5           # against the commit before this task
CODE CHANGED (not comment-only): src/setuhaul/auth/routes.py
CODE CHANGED (not comment-only): src/setuhaul/auth/same_origin.py
CODE CHANGED (not comment-only): tests/test_auth_google.py
CODE CHANGED (not comment-only): tests/test_auth_hardening_pages.py
4 changed file(s) checked, 4 with code changes                       (exit 1)

$ PYTHONPATH=src $PY -m pytest -q -p no:cacheprovider
1158 passed in 70.35s (0:01:10)
```

`check_comment_only.py` proves that a **documentation-only** pass changed no behaviour. This feature changes code, so it does not apply as a
gate: exit 1 against an earlier base is the expected, correct answer (against the branch point `8d16129` it also exits 1: 56 files checked,
10 with code changes). The exit 0 against HEAD only says that nothing under `src`, `scripts` or `tests` was uncommitted.

## End-to-end smoke

Command (the script is reproduced below; save it anywhere as `auth_smoke.py` and run it from the repository root):

```text
$ PYTHONUTF8=1 PYTHONPATH=src $PY <scratch>/auth_smoke.py
```

### First run: 1 check failed (exit 1)

The failure was a bug in the smoke script, not in the service: it queried a table named `dock_slots`, which does not exist (the table is
`appointment_slots`). The leak check in that run therefore did not include slot ids.

```text
== 1. bootstrap admin signs in
PASS  callback redirects  -> 302
PASS  signed in as ADMIN  -> {'email': 'boss@example.test', 'role': 'ADMIN'}
PASS  / sends the admin to /admin
PASS  /admin renders
PASS  uninvited address refused  -> (403, 'NOT_INVITED')
== 2. admin invites a warehouse authority
PASS  invite  -> 201
PASS  warehouse authority signs in
PASS  /warehouse renders
PASS  /admin refused to warehouse authority
PASS  own board
PASS  other facility's board looks missing  -> FACILITY_NOT_FOUND
PASS  same host over http is refused as cross-site  -> CROSS_SITE_REQUEST
== 3. warehouse authority adds a driver
PASS  driver added, pending  -> {'status': 'OK', 'driver_id': 'DRV016', 'approval_status': 'PENDING_APPROVAL', 'request_id': 'a125ddc587554cbea13cf73b407b7e70'}
== 3b. a pending driver cannot link
      webhook HTTP 200 keys=['request_id', 'status']; bot said: ["Sorry, I can't link or help this account. Please contact your warehouse coordinator."]
PASS  refused while pending
== 4. admin approves
PASS  driver is in the pending list
PASS  approved  -> APPROVED
== 5. warehouse authority creates a project and allocates
PASS  project created  -> 201
PASS  driver allocated  -> 200
== 6. fixture Telegram updates
PASS  wrong secret refused  -> INVALID_SECRET
      webhook HTTP 200 keys=['request_id', 'status']; bot said: ['To use SetuHaul, please share your phone number with the button below so I can link this chat to your driver profile.']
PASS  unlinked sender is only asked for the number
      webhook HTTP 200 keys=['request_id', 'status']; bot said: ["Sorry, I can't link or help this account. Please contact your warehouse coordinator."]
PASS  someone else's contact card refused
      webhook HTTP 200 keys=['request_id', 'status']; bot said: ['Thanks, this chat is now linked to your driver profile. How can I help?']
PASS  driver bound
PASS  binding row
      webhook HTTP 200 keys=['request_id', 'status']; bot said: ['How late are you, and which shipment is it?']
PASS  driver answered
      webhook HTTP 200 keys=['request_id', 'status']; bot said: ['How late are you, and which shipment is it?']
PASS  board request answered once
      webhook HTTP 200 keys=['request_id', 'status']; bot said: []
PASS  a repeated update is not answered twice
== 7. nothing from the bot carries board data
FAIL  query ran: select slot_id from dock_slots  -> no such table: dock_slots
PASS  none of 117 board values appears in 6 bot messages
PASS  no dock code or slot time in bot messages
PASS  webhook bodies carry only status and request id
PASS  no session, no data: /v1/warehouse/board?facility_id=FAC-JAI-01  -> 401
PASS  no session, no data: /v1/warehouse/drivers  -> 401
PASS  no session, no data: /v1/coordinator/cases  -> 401
== 8. the warehouse authority sees the project driver's chat
PASS  only the allocated driver  -> ['DRV016']
== 9. admin disables the warehouse authority
PASS  disabled  -> 200
PASS  old session refused: /auth/me  -> 401
PASS  old session refused: /v1/warehouse/board?facility_id=FAC-JAI-01  -> 401
PASS  old session refused: /v1/warehouse/driver-chats  -> 401
PASS  old session refused: /v1/projects  -> 401
PASS  screen sends the old session to /  -> 303
PASS  old session cannot write  -> 401
PASS  new sign-in refused  -> DISABLED
      webhook HTTP 200 keys=['request_id', 'status']; bot said: ['How late are you, and which shipment is it?']
PASS  the driver (not disabled) is still answered
== 10. audit trail
      ACCESS_DENIED FORBIDDEN
      BOOTSTRAP_ADMIN OK
      CROSS_SITE_REQUEST FORBIDDEN
      DISABLE OK
      DRIVER_ADDED OK
      DRIVER_ALLOCATED OK
      DRIVER_APPROVED OK
      INVITE OK
      LOGIN LOGIN_OK
      LOGIN LOGIN_REFUSED
      PROJECT_CREATED OK
      TELEGRAM_BIND_OK OK
      TELEGRAM_BIND_REFUSED REFUSED
PASS  audit holds no phone number or token

RESULT: 1 FAILED: ['query ran: select slot_id from dock_slots']
```

### Second run, after changing only that table name (exit 0)

```text
== 1. bootstrap admin signs in
PASS  callback redirects  -> 302
PASS  signed in as ADMIN  -> {'email': 'boss@example.test', 'role': 'ADMIN'}
PASS  / sends the admin to /admin
PASS  /admin renders
PASS  uninvited address refused  -> (403, 'NOT_INVITED')
== 2. admin invites a warehouse authority
PASS  invite  -> 201
PASS  warehouse authority signs in
PASS  /warehouse renders
PASS  /admin refused to warehouse authority
PASS  own board
PASS  other facility's board looks missing  -> FACILITY_NOT_FOUND
PASS  same host over http is refused as cross-site  -> CROSS_SITE_REQUEST
== 3. warehouse authority adds a driver
PASS  driver added, pending  -> {'status': 'OK', 'driver_id': 'DRV016', 'approval_status': 'PENDING_APPROVAL', 'request_id': '6a3df946c251478782f32fba7fa95b36'}
== 3b. a pending driver cannot link
      webhook HTTP 200 keys=['request_id', 'status']; bot said: ["Sorry, I can't link or help this account. Please contact your warehouse coordinator."]
PASS  refused while pending
== 4. admin approves
PASS  driver is in the pending list
PASS  approved  -> APPROVED
== 5. warehouse authority creates a project and allocates
PASS  project created  -> 201
PASS  driver allocated  -> 200
== 6. fixture Telegram updates
PASS  wrong secret refused  -> INVALID_SECRET
      webhook HTTP 200 keys=['request_id', 'status']; bot said: ['To use SetuHaul, please share your phone number with the button below so I can link this chat to your driver profile.']
PASS  unlinked sender is only asked for the number
      webhook HTTP 200 keys=['request_id', 'status']; bot said: ["Sorry, I can't link or help this account. Please contact your warehouse coordinator."]
PASS  someone else's contact card refused
      webhook HTTP 200 keys=['request_id', 'status']; bot said: ['Thanks, this chat is now linked to your driver profile. How can I help?']
PASS  driver bound
PASS  binding row
      webhook HTTP 200 keys=['request_id', 'status']; bot said: ['How late are you, and which shipment is it?']
PASS  driver answered
      webhook HTTP 200 keys=['request_id', 'status']; bot said: ['How late are you, and which shipment is it?']
PASS  board request answered once
      webhook HTTP 200 keys=['request_id', 'status']; bot said: []
PASS  a repeated update is not answered twice
== 7. nothing from the bot carries board data
PASS  none of 223 board values appears in 6 bot messages
PASS  no dock code or slot time in bot messages
PASS  webhook bodies carry only status and request id
PASS  no session, no data: /v1/warehouse/board?facility_id=FAC-JAI-01  -> 401
PASS  no session, no data: /v1/warehouse/drivers  -> 401
PASS  no session, no data: /v1/coordinator/cases  -> 401
== 8. the warehouse authority sees the project driver's chat
PASS  only the allocated driver  -> ['DRV016']
== 9. admin disables the warehouse authority
PASS  disabled  -> 200
PASS  old session refused: /auth/me  -> 401
PASS  old session refused: /v1/warehouse/board?facility_id=FAC-JAI-01  -> 401
PASS  old session refused: /v1/warehouse/driver-chats  -> 401
PASS  old session refused: /v1/projects  -> 401
PASS  screen sends the old session to /  -> 303
PASS  old session cannot write  -> 401
PASS  new sign-in refused  -> DISABLED
      webhook HTTP 200 keys=['request_id', 'status']; bot said: ['How late are you, and which shipment is it?']
PASS  the driver (not disabled) is still answered
== 10. audit trail
      ACCESS_DENIED FORBIDDEN
      BOOTSTRAP_ADMIN OK
      CROSS_SITE_REQUEST FORBIDDEN
      DISABLE OK
      DRIVER_ADDED OK
      DRIVER_ALLOCATED OK
      DRIVER_APPROVED OK
      INVITE OK
      LOGIN LOGIN_OK
      LOGIN LOGIN_REFUSED
      PROJECT_CREATED OK
      TELEGRAM_BIND_OK OK
      TELEGRAM_BIND_REFUSED REFUSED
PASS  audit holds no phone number or token

RESULT: all checks passed
```

## What the run does not show

* After the warehouse authority was disabled the driver was still answered: disabling a web user does not touch drivers or projects.
* The bot's answers came from the rule-based stand-in model (`SETUHAUL_USE_STUB_MODEL=1`). The leak check proves that these replies carried
  none of the listed board values; it does not show how a real model would word a reply. The protection there is that the conversation
  engine's tools are limited to the driver's own records.
* The driver added here has no loads, so no booking was made over Telegram.
* Real Google, a real bot, a real HTTPS proxy, real Redis and the browser screens' scripts were not exercised.

## The script

```python
"""End-to-end smoke of auth and roles through create_app_from_env, with fake Google and Telegram and no network."""

import os
import re
import socket
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Refuse any connection that leaves this machine, so a mistake cannot reach a real service.
_connect = socket.socket.connect


def _guard(sock, address):
    if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1"):
        raise RuntimeError(f"network blocked: {address[0]}")
    return _connect(sock, address)


socket.socket.connect = _guard

# Nothing from the caller's environment leaks in.
for key in [k for k in os.environ if k.startswith(("SETUHAUL_", "LANGSMITH", "LANGCHAIN"))]:
    del os.environ[key]

from fastapi.testclient import TestClient  # noqa: E402

from setuhaul.api import create_app_from_env  # noqa: E402
from setuhaul.auth.google import FakeGoogleVerifier, GoogleIdentity  # noqa: E402
from setuhaul.auth.telegram import FakeTelegramClient  # noqa: E402
from setuhaul.db import make_working_db, open_connection  # noqa: E402

BASE = "https://haul.example.test"
TG_SECRET = "smoke-telegram-secret-not-real"
work = Path(tempfile.mkdtemp(prefix="setuhaul-smoke-"))
db = make_working_db(work / "smoke.sqlite3")
os.environ.update({
    "SETUHAUL_DB_PATH": str(db), "SETUHAUL_REDIS_URL": "fakeredis://", "SETUHAUL_USE_STUB_MODEL": "1",
    "SETUHAUL_FAKE_NOW": "2026-08-04T09:35:00+05:30", "SETUHAUL_ENV_FILE": str(work / "none.env"),
    "SETUHAUL_FALLBACK_ESCALATIONS": str(work / "fallback.jsonl"), "SETUHAUL_PUBLIC_URL": BASE,
    "SETUHAUL_GOOGLE_CLIENT_ID": "fake-client-id", "SETUHAUL_GOOGLE_CLIENT_SECRET": "fake-client-secret",
    "SETUHAUL_SESSION_SECRET": "smoke-session-secret-not-real-0000000", "SETUHAUL_BOOTSTRAP_ADMIN_EMAIL": "boss@example.test",
    "SETUHAUL_TELEGRAM_BOT_TOKEN": "fake-bot-token", "SETUHAUL_TELEGRAM_WEBHOOK_SECRET": TG_SECRET,
})

app = create_app_from_env()
deps = app.state.deps
google, bot = FakeGoogleVerifier(), FakeTelegramClient()
deps.google, deps.telegram = google, bot  # swap the real clients for fakes before any request

failures: list[str] = []


def check(label: str, ok: bool, shown: object = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  -> {shown}" if shown != "" else ""))
    if not ok:
        failures.append(label)


def client() -> TestClient:
    return TestClient(app, base_url=BASE, follow_redirects=False)


def sign_in(c: TestClient, email: str):
    google.identity = GoogleIdentity(email, True, email.split("@")[0])
    start = c.get("/auth/login")
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    return c.get("/auth/callback", params={"code": "fake-code", "state": state})


def post(c: TestClient, path: str, body: dict):
    return c.post(path, json=body, headers={"origin": BASE})


def query(sql: str, *args):
    conn = open_connection(path=db, readonly=True)
    try:
        return [tuple(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


print("== 1. bootstrap admin signs in")
admin = client()
r = sign_in(admin, "boss@example.test")
check("callback redirects", r.status_code == 302, r.status_code)
me = admin.get("/auth/me").json()
check("signed in as ADMIN", me.get("role") == "ADMIN", {k: me.get(k) for k in ("email", "role")})
check("/ sends the admin to /admin", admin.get("/").headers.get("location") == "/admin")
check("/admin renders", admin.get("/admin").status_code == 200)
stranger = client()
r = sign_in(stranger, "nobody@example.test")
check("uninvited address refused", r.status_code == 403 and r.json()["reason_code"] == "NOT_INVITED", (r.status_code, r.json()["reason_code"]))

print("== 2. admin invites a warehouse authority")
r = post(admin, "/v1/admin/users", {"email": "wh@example.test", "role": "WAREHOUSE_AUTHORITY", "display_name": "Smoke Warehouse",
                                    "facilities": ["FAC-JAI-01"]})
check("invite", r.status_code == 201, r.status_code)
wh_id = r.json()["user"]["user_id"]
wh = client()
check("warehouse authority signs in", sign_in(wh, "wh@example.test").status_code == 302)
check("/warehouse renders", wh.get("/warehouse").status_code == 200)
check("/admin refused to warehouse authority", wh.get("/admin").status_code == 403)
check("own board", wh.get("/v1/warehouse/board", params={"facility_id": "FAC-JAI-01"}).status_code == 200)
r = wh.get("/v1/warehouse/board", params={"facility_id": "FAC-GGN-01"})
check("other facility's board looks missing", r.status_code == 404, r.json()["reason_code"])
r = wh.post("/v1/projects", json={}, headers={"origin": "http://haul.example.test"})
check("same host over http is refused as cross-site", r.status_code == 403 and r.json()["reason_code"] == "CROSS_SITE_REQUEST", r.json()["reason_code"])

print("== 3. warehouse authority adds a driver")
vehicle, carrier = query("select vehicle_id, carrier_id from vehicles where active_flag=1 order by vehicle_id limit 1")[0]
phone = "+919812345678"
r = post(wh, "/v1/drivers", {"name": "Smoke Driver", "phone": phone, "licence_number": "RJ14-SMOKE-0001",
                             "licence_expiry": "2028-01-31", "carrier_id": carrier, "vehicle_id": vehicle})
check("driver added, pending", r.status_code == 201 and r.json()["approval_status"] == "PENDING_APPROVAL", r.json())
driver_id = r.json()["driver_id"]


def update(n: int, uid: int, **message):
    return {"update_id": n, "message": {"message_id": n, "from": {"id": uid, "is_bot": False},
                                        "chat": {"id": uid, "type": "private"}, "date": 1, **message}}


hook_bodies: list[dict] = []


def hook(body: dict):
    before = len(bot.sent)
    r = client().post("/v1/telegram/webhook", json=body, headers={"x-telegram-bot-api-secret-token": TG_SECRET})
    hook_bodies.append(r.json())
    replies = [text for _, text, _ in bot.sent[before:]]
    print(f"      webhook HTTP {r.status_code} keys={sorted(r.json())}; bot said: {replies}")
    return r, replies


UID = 555001
share = {"contact": {"phone_number": phone.lstrip("+"), "first_name": "S", "user_id": UID}}
print("== 3b. a pending driver cannot link")
r, replies = hook(update(1, UID, **share))
check("refused while pending", len(replies) == 1 and "contact your warehouse" in replies[0])

print("== 4. admin approves")
pending = admin.get("/v1/drivers/pending").json()["drivers"]
check("driver is in the pending list", [d["driver_id"] for d in pending] == [driver_id])
r = post(admin, f"/v1/drivers/{driver_id}/approve", {})
check("approved", r.status_code == 200, r.json().get("approval_status"))

print("== 5. warehouse authority creates a project and allocates")
r = post(wh, "/v1/projects", {"customer_name": "Smoke Customer", "contract_ref": "SMOKE-1", "facility_id": "FAC-JAI-01",
                              "starts_on": "2026-08-04"})
check("project created", r.status_code == 201, r.status_code)
project_id = r.json()["project_id"]
r = post(wh, f"/v1/projects/{project_id}/allocate", {"driver_id": driver_id})
check("driver allocated", r.status_code == 200, r.status_code)

print("== 6. fixture Telegram updates")
r = client().post("/v1/telegram/webhook", json=update(2, UID, text="hi"), headers={"x-telegram-bot-api-secret-token": "wrong"})
check("wrong secret refused", r.status_code == 401, r.json()["reason_code"])
r, replies = hook(update(3, UID, text="show me the dock board"))
check("unlinked sender is only asked for the number", len(replies) == 1 and "share your phone number" in replies[0])
r, replies = hook(update(4, 555999, contact={"phone_number": phone, "first_name": "S", "user_id": UID}))
check("someone else's contact card refused", len(replies) == 1 and "contact your warehouse" in replies[0])
r, replies = hook(update(5, UID, **share))
check("driver bound", r.status_code == 200 and len(replies) == 1 and "linked" in replies[0])
check("binding row", query("select driver_id from telegram_identities where revoked_at is null") == [(driver_id,)])
r, replies = hook(update(6, UID, text="my truck is 60 minutes late"))
check("driver answered", r.status_code == 200 and len(replies) == 1 and bool(replies[0]))
r, replies = hook(update(7, UID, text="show me the dock board for FAC-JAI-01 with every driver and booking"))
check("board request answered once", r.status_code == 200 and len(replies) == 1)
r, replies = hook(update(7, UID, text="show me the dock board for FAC-JAI-01 with every driver and booking"))
check("a repeated update is not answered twice", r.status_code == 200 and replies == [])

print("== 7. nothing from the bot carries board data")
secrets_of_others = {v for (v,) in query("select driver_name from drivers where driver_id<>?", driver_id)}
for sql in ("select driver_id from drivers", "select shipment_id from shipments", "select order_reference from shipments",
            "select customer_name from shipments", "select appointment_id from appointments", "select dock_id from docks",
            "select slot_id from appointment_slots", "select facility_id from facilities", "select facility_name from facilities"):
    try:
        secrets_of_others |= {str(v) for (v,) in query(sql) if v}
    except Exception as exc:  # a wrong table name must show up, not be skipped
        check(f"query ran: {sql}", False, exc)
secrets_of_others.discard(driver_id)
said = "\n".join(text for _, text, _ in bot.sent)
leaked = sorted(v for v in secrets_of_others if v in said)
check(f"none of {len(secrets_of_others)} board values appears in {len(bot.sent)} bot messages", not leaked, leaked or "")
check("no dock code or slot time in bot messages", not re.search(r"\bD[1-9]\b|\b\d{2}:\d{2}\b", said))
check("webhook bodies carry only status and request id", all(set(b) <= {"status", "request_id", "reason_code"} for b in hook_bodies))
for path in ("/v1/warehouse/board?facility_id=FAC-JAI-01", "/v1/warehouse/drivers", "/v1/coordinator/cases"):
    r = client().get(path, headers={"x-telegram-bot-api-secret-token": TG_SECRET})
    check(f"no session, no data: {path}", r.status_code == 401, r.status_code)

print("== 8. the warehouse authority sees the project driver's chat")
chats = wh.get("/v1/warehouse/driver-chats").json()["drivers"]
check("only the allocated driver", [c["driver_id"] for c in chats] == [driver_id], [c["driver_id"] for c in chats])

print("== 9. admin disables the warehouse authority")
r = post(admin, f"/v1/admin/users/{wh_id}/disable", {})
check("disabled", r.status_code == 200, r.status_code)
for path in ("/auth/me", "/v1/warehouse/board?facility_id=FAC-JAI-01", "/v1/warehouse/driver-chats", "/v1/projects"):
    r = wh.get(path)
    check(f"old session refused: {path}", r.status_code == 401, r.status_code)
r = wh.get("/warehouse")
check("screen sends the old session to /", r.status_code == 303 and r.headers["location"] == "/", r.status_code)
r = post(wh, f"/v1/projects/{project_id}/release", {"driver_id": driver_id})
check("old session cannot write", r.status_code == 401, r.status_code)
r = sign_in(client(), "wh@example.test")
check("new sign-in refused", r.status_code == 403 and r.json()["reason_code"] == "DISABLED", r.json()["reason_code"])
r, replies = hook(update(8, UID, text="hello again"))
check("the driver (not disabled) is still answered", len(replies) == 1)

print("== 10. audit trail")
entries = admin.get("/v1/admin/audit", params={"limit": 500}).json()["entries"]
seen = sorted({(e["action"], e["outcome"]) for e in entries})
for pair in seen:
    print("     ", *pair)
check("audit holds no phone number or token", not any(s in str(entries) for s in (phone[1:], "fake-bot-token", TG_SECRET)))

print(f"\nRESULT: {'all checks passed' if not failures else f'{len(failures)} FAILED: {failures}'}")
sys.exit(1 if failures else 0)
```
