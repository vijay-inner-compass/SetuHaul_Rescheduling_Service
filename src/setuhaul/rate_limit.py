"""Per-driver, per-channel message rate limits backed by Redis counters.

Fixed windows keyed by the injected clock. Denied messages are rolled back so they do not consume
budget. The increment/rollback pair is not atomic across processes; that approximation is
acceptable for an abuse limit and is documented rather than hidden.
"""

# Lazy annotations.
from __future__ import annotations

# The Redis client type (real Redis in production, fakeredis in tests and the demo).
import logging
import threading

# dataclass for small immutable value objects.
from dataclasses import dataclass

from redis import Redis
from redis.exceptions import RedisError

# Clock is injected so tests and the demo control which window a message falls in.
from setuhaul.clock import Clock

# Default limits come from the central settings.
from setuhaul.config import load_settings


# The two ceilings applied to each driver.
@dataclass(frozen=True)
class RateLimits:
    per_minute: int
    per_hour: int

    @classmethod
    def from_settings(cls) -> RateLimits:
        # Build the limits from settings (20 a minute, 100 an hour by default).
        s = load_settings()
        return cls(s.rate_limit_per_minute, s.rate_limit_per_hour)


# The answer to "may this message proceed?".
@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    # Which limit was hit (RATE_LIMIT_MINUTE or RATE_LIMIT_HOUR); None when allowed.
    reason_code: str | None = None
    # Seconds until the window resets, so the driver can be told when to retry.
    retry_after_seconds: int = 0


def _count(
    redis: Redis,
    driver_id: str,
    clock: Clock,
    limits: RateLimits | None = None,
    channel: str = "default",
) -> RateLimitResult:
    # Use the caller's limits if given (tests do), otherwise the configured ones.
    limits = limits or RateLimits.from_settings()
    # Whole seconds since 1970; dividing by 60 or 3600 below yields the current minute or hour "window" number.
    epoch = int(clock.now().timestamp())
    # One counter per driver per channel per window. A new window means a new key, so the count resets by itself.
    m_key = f"setuhaul:rl:{channel}:{driver_id}:m:{epoch // 60}"
    h_key = f"setuhaul:rl:{channel}:{driver_id}:h:{epoch // 3600}"
    # A pipeline sends the four commands in one round trip.
    pipe = redis.pipeline()
    # Count this message in the minute window, and let that counter expire after 2 minutes so old keys vanish.
    pipe.incr(m_key)
    pipe.expire(m_key, 120)
    # Same for the hour window, expiring after 2 hours.
    pipe.incr(h_key)
    pipe.expire(h_key, 7200)
    # Run them and keep only the two counts (the expire replies are ignored).
    minute_count, _, hour_count, _ = pipe.execute()
    # Over the minute limit: undo the hour increment (this message did not really happen) and report the wait.
    if minute_count > limits.per_minute:
        redis.decr(h_key)
        return RateLimitResult(False, "RATE_LIMIT_MINUTE", 60 - epoch % 60)
    # Over the hour limit: undo both increments so a denied message does not use up budget.
    if hour_count > limits.per_hour:
        redis.decr(m_key)
        redis.decr(h_key)
        return RateLimitResult(False, "RATE_LIMIT_HOUR", 3600 - epoch % 3600)
    # Within both limits.
    return RateLimitResult(True)


# The limits protect against abuse; they are not worth silencing every driver when Redis is down, so an outage lets
# messages through (the sender is still a verified driver, and each message has its own model cost stop).
def check_rate_limit(
    redis: Redis,
    driver_id: str,
    clock: Clock,
    limits: RateLimits | None = None,
    channel: str = "default",
) -> RateLimitResult:
    try:
        return _count(redis, driver_id, clock, limits, channel)
    except RedisError:
        logging.getLogger(__name__).warning("rate limit not checked: Redis unreachable")
        return RateLimitResult(True)


# Default sign-in throttle (D3): 10 attempts per address per 15 minutes.
AUTH_RATE_LIMIT = 10
AUTH_RATE_WINDOW_SECONDS = 900
_FALLBACK_MAX_KEYS = 10_000


class AuthRateLimitFallback:
    """In-process per-address counter used only when Redis is unreachable, so sign-in bursts are still throttled
    without locking break-glass admins out during a Redis outage."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def check(self, key: str, limit: int, retry_after: int) -> RateLimitResult:
        with self._lock:
            if len(self._counts) >= _FALLBACK_MAX_KEYS and key not in self._counts:
                self._counts.clear()
            count = self._counts.get(key, 0) + 1
            if count > limit:
                return RateLimitResult(False, "RATE_LIMITED", retry_after)
            self._counts[key] = count
            return RateLimitResult(True)


def check_auth_rate_limit(
    redis: Redis,
    ip: str | None,
    bucket: str,
    clock: Clock,
    *,
    limit: int = AUTH_RATE_LIMIT,
    window_seconds: int = AUTH_RATE_WINDOW_SECONDS,
    fallback: AuthRateLimitFallback | None = None,
) -> RateLimitResult:
    """Per-address fixed-window rate limit for sign-in routes. Falls back to an in-process counter if Redis is down."""
    addr = ip or "unknown"
    epoch = int(clock.now().timestamp())
    window = epoch // window_seconds
    retry_after = max(1, window_seconds - (epoch % window_seconds))
    key = f"setuhaul:auth_rl:{bucket}:{addr}:{window}"
    try:
        pipe = redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_seconds * 2)
        count, _ = pipe.execute()
        if int(count) > limit:
            return RateLimitResult(False, "RATE_LIMITED", retry_after)
        return RateLimitResult(True)
    except RedisError:
        logging.getLogger(__name__).warning("auth rate limit using in-process fallback: Redis unreachable")
        if fallback is None:
            return RateLimitResult(False, "RATE_LIMITED", retry_after)
        return fallback.check(key, limit, retry_after)
