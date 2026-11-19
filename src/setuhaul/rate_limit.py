"""Per-driver, per-channel message rate limits backed by Redis counters.

Fixed windows keyed by the injected clock. Denied messages are rolled back so they do not consume
budget. The increment/rollback pair is not atomic across processes; that approximation is
acceptable for an abuse limit and is documented rather than hidden.
"""

from __future__ import annotations

from dataclasses import dataclass

from redis import Redis

from setuhaul.clock import Clock
from setuhaul.config import load_settings


@dataclass(frozen=True)
class RateLimits:
    per_minute: int
    per_hour: int

    @classmethod
    def from_settings(cls) -> RateLimits:
        s = load_settings()
        return cls(s.rate_limit_per_minute, s.rate_limit_per_hour)


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    reason_code: str | None = None
    retry_after_seconds: int = 0


def check_rate_limit(
    redis: Redis,
    driver_id: str,
    clock: Clock,
    limits: RateLimits | None = None,
    channel: str = "default",
) -> RateLimitResult:
    limits = limits or RateLimits.from_settings()
    epoch = int(clock.now().timestamp())
    m_key = f"setuhaul:rl:{channel}:{driver_id}:m:{epoch // 60}"
    h_key = f"setuhaul:rl:{channel}:{driver_id}:h:{epoch // 3600}"
    pipe = redis.pipeline()
    pipe.incr(m_key)
    pipe.expire(m_key, 120)
    pipe.incr(h_key)
    pipe.expire(h_key, 7200)
    minute_count, _, hour_count, _ = pipe.execute()
    if minute_count > limits.per_minute:
        redis.decr(h_key)
        return RateLimitResult(False, "RATE_LIMIT_MINUTE", 60 - epoch % 60)
    if hour_count > limits.per_hour:
        redis.decr(m_key)
        redis.decr(h_key)
        return RateLimitResult(False, "RATE_LIMIT_HOUR", 3600 - epoch % 3600)
    return RateLimitResult(True)
