# 2. Identity, memory and limits

Files: `identity.py`, `memory.py`, `rate_limit.py`, `observability.py`.

## Identity (`identity.py`)

**Goal:** the model must never be able to say who the driver is.

1. `verify_channel_request` checks an HMAC-SHA256 signature over the **raw request bytes before anything is parsed**.
   `hmac.compare_digest` takes the same time however many characters match, so the signature cannot be guessed by timing.
2. `normalize_phone` turns "98765 43210", "+91 98765 43210" and "09876543210" into one E.164 form.
   **Rejected:** wrong lengths, other formats (`INVALID_PHONE`).
3. `provision_identities` fills `channel_identities` from `drivers.phone` (idempotent). `resolve_driver` maps a verified
   number to a `TrustedDriverContext`. An unknown number is `UNKNOWN_PHONE`; a suspended driver is `DRIVER_NOT_ALLOWED`.
4. Sessions (`create_session`, `authenticate_session`): a random 32-byte token is returned **once**; only its SHA-256 hash
   is stored. A client-supplied driver id is never a valid token. Revoked and expired sessions are refused, and the
   driver's status is re-checked on every use.
5. `authorize_shipment` returns the **same error** for a missing shipment and someone else's shipment (`FOREIGN_SHIPMENT`),
   so shipment ids cannot be probed.

## Memory (`memory.py`)

* **SQL is the truth; Redis is a cache.** Every message is written to `chat_messages` and committed first. The cache holds
  a bounded recent window (20 turns, 12,000 characters, 24 hours) under a key scoped to **both** driver and thread.
* Lose the cache and nothing is lost: `load` rebuilds from SQL (`from_cache=False`).
* `ConversationState` (selected shipment, latest option set, hold, language, pending events) is stored in
  `conversation_state` and is **never trimmed** by the turn caps.
* **Holds are verified, not remembered.** On every load, a remembered hold is checked against the database through the
  hold checker. If it lapsed, the state forgets it and records a `HOLD_EXPIRED` event so the driver is told exactly once.
* Ownership: another driver's thread is `FOREIGN_THREAD`; selecting another driver's shipment is `FOREIGN_SHIPMENT` and
  leaves the state unchanged.

## Rate limiting (`rate_limit.py`)

* Fixed windows in Redis, keyed by the injected clock: per driver, per channel, per minute (20) and per hour (100).
* A denied message is rolled back (`decr`), so refused messages do not use up budget.
* Documented limitation: the increment/rollback pair is not atomic across processes. That is acceptable for an abuse
  limit and is stated rather than hidden.

## Observability (`observability.py`)

* Every request gets an id; spans time each step and carry a reason code when something unusual happened.
* `redact` masks secret-named keys and phone-number-like text before anything is logged.

## Edge cases and failure scenarios

| Situation | Result | Test |
|---|---|---|
| Wrong or missing signature | 401, body never parsed | `test_invalid_signature_rejected_before_parsing`, `test_bad_or_missing_signature_is_refused_before_the_body_is_parsed` |
| Phone in an odd format | normalised to one form | `test_normalize_phone_to_e164` |
| Garbage phone | `INVALID_PHONE` | `test_normalize_phone_rejects_garbage` |
| Unknown number / suspended driver | refused | `test_unknown_phone_rejected`, `test_suspended_driver_rejected` |
| Session token stolen from the database | useless: only a hash is stored | `test_raw_token_is_not_stored` |
| Client sends a driver id as a token | refused | `test_client_supplied_session_id_is_never_authorization` |
| 21st message in a minute | 429 with `Retry-After` | `test_message_burst_blocked_after_per_minute_limit` |
| Denied messages and the hourly budget | not charged | `test_denied_messages_do_not_consume_hourly_budget` |
| Redis restarts mid-conversation | rebuilt from SQL | `test_redis_loss_rebuilds_from_sql` |
| One huge message | truncated, not dropped | `test_single_oversized_turn_is_truncated_not_dropped` |
| Hold lapses while the driver is away | event once, no stale promise | `test_return_at_or_after_expiry_yields_hold_expired_not_stale_promise` |
