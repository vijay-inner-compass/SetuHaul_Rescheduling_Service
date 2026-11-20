"""Injectable time source. Services never call datetime.now() directly."""

# Makes annotations lazy strings, so type hints may name classes defined later in the file.
from __future__ import annotations

# datetime is the timezone-aware point in time we pass around; timedelta is the step FakeClock.advance takes.
from datetime import datetime, timedelta

# Protocol lets Clock be a structural type: anything with a now() method qualifies, no inheritance needed.
from typing import Protocol

# IST is the one operational timezone (Asia/Kolkata); every clock reports time in it so comparisons are consistent.
from setuhaul.db import IST


# The contract every service depends on. Services take a Clock argument instead of reading the wall clock,
# which is what makes hold expiry, rate limits and the demo's simulated time testable without sleeping.
class Clock(Protocol):
    # Return "now" as an aware datetime. The "..." body means the protocol only declares the shape.
    def now(self) -> datetime: ...


# The production clock: the real time, in IST.
class SystemClock:
    def now(self) -> datetime:
        # datetime.now(IST) is already timezone-aware, so it compares safely with stored timestamps.
        return datetime.now(IST)


class FakeClock:
    """Deterministic clock for tests; advancing it never sleeps."""

    def __init__(self, start: datetime) -> None:
        # A naive datetime has no offset; accepting it would silently mix timezones, so refuse it up front.
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("FakeClock requires an aware datetime")
        # Normalise to IST so every reading from this clock has the same offset regardless of what was passed in.
        self._now = start.astimezone(IST)

    def now(self) -> datetime:
        # Returns the stored instant; it only changes when advance() or set() is called.
        return self._now

    def advance(self, delta: timedelta) -> None:
        # Moves time forward (or back, for a negative delta) instantly. This is how tests expire a 180 s hold.
        self._now += delta

    def set(self, value: datetime) -> None:
        # Same awareness rule as the constructor: a naive value is rejected rather than guessed at.
        if value.tzinfo is None:
            raise ValueError("FakeClock requires an aware datetime")
        # Jump to an absolute time, again normalised to IST.
        self._now = value.astimezone(IST)
