"""Locust profile: drivers sending delay messages to the API with the stub model.

Spike profile (see docs/verification/scale-test.md): 35 messages in 35 s. The brief's spike is 35 messages over
30 minutes, so this is compressed by a factor of about 51 (1800 s / 35 s). Start the server first:

    SETUHAUL_DB_PATH=<scale db> SETUHAUL_REDIS_URL=fakeredis:// SETUHAUL_USE_STUB_MODEL=1 \
    SETUHAUL_WEBHOOK_SECRET=load-secret python -m uvicorn --factory setuhaul.api:create_app_from_env --port 8765

    SETUHAUL_WEBHOOK_SECRET=load-secret locust -f load/locustfile.py --headless -u 1 -r 1 -t 35s \
        --host http://127.0.0.1:8765 --csv docs/verification/locust
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import os
import random

from locust import HttpUser, constant, task

SECRET = os.environ.get("SETUHAUL_WEBHOOK_SECRET", "load-secret").encode()
_drivers = itertools.count(20)  # GEN-DRV0020 onwards: scheduled loads outside the burst group
_rng = random.Random(11)


class DriverUser(HttpUser):
    wait_time = constant(1.0)  # one message a second

    @task
    def delay_message(self) -> None:
        i = next(_drivers)
        body = json.dumps({"from": f"+91-91{i:08d}", "message_id": f"LOC-{i}-{_rng.randint(0, 10**9)}",
                           "text": f"my truck is {_rng.choice([30, 45, 60, 90])} minutes late"}).encode()
        sig = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
        with self.client.post("/v1/driver/messages", data=body, headers={"x-signature": sig, "content-type": "application/json"},
                              name="POST /v1/driver/messages", catch_response=True) as r:
            if r.status_code != 200 or r.json().get("status") not in ("REPLIED", "HANDOFF"):
                r.failure(f"{r.status_code} {r.text[:120]}")
