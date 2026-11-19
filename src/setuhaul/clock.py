"""Injectable time source. Services never call datetime.now() directly."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from setuhaul.db import IST


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(IST)


class FakeClock:
    """Deterministic clock for tests; advancing it never sleeps."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("FakeClock requires an aware datetime")
        self._now = start.astimezone(IST)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("FakeClock requires an aware datetime")
        self._now = value.astimezone(IST)
