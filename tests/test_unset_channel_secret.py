"""With no channel secret configured, the signed driver and GPS channel accepts nothing."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

from setuhaul.api import AppDeps, create_app
from setuhaul.db import make_working_db, open_connection
from setuhaul.identity import AuthError, provision_identities, verify_channel_request


def _sign(body: bytes, secret: bytes) -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def test_a_signature_made_with_an_empty_key_is_refused() -> None:
    body = b'{"from": "x"}'
    with pytest.raises(AuthError) as refused:
        verify_channel_request(body, _sign(body, b""), b"")
    assert refused.value.code == "INVALID_SIGNATURE"


@pytest.mark.parametrize("path,payload", [
    ("/v1/driver/messages", {"message_id": "M-1", "text": "my truck is 60 minutes late"}),
    ("/v1/driver/location", {"lat": 27.4, "lon": 75.79, "recorded_at": "2026-08-04T09:34:00+05:30"}),
])
def test_a_forged_driver_request_is_refused_when_no_secret_is_set(tmp_path: Path, path: str, payload: dict[str, object]) -> None:
    db = make_working_db(tmp_path / "w.sqlite3")
    conn = open_connection(path=db)
    provision_identities(conn)
    phone = conn.execute("select phone from drivers where driver_id='DRV006'").fetchone()[0]
    conn.close()
    deps = AppDeps(db_path=db, redis=fakeredis.FakeRedis(decode_responses=True), webhook_secret=b"", coordinator_tokens={},
                   fallback_path=tmp_path / "f.jsonl")
    body = json.dumps({"from": phone, **payload}).encode()
    r = TestClient(create_app(deps)).post(path, content=body, headers={"x-signature": _sign(body, b"")})
    assert r.status_code == 401 and r.json()["reason_code"] == "INVALID_SIGNATURE", r.text
