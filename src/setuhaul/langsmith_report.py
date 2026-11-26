"""A read-only summary of recent LangSmith runs for the admin Model tab, fetched only when an admin asks.

It asks for times, errors and status only: no inputs or outputs. It never switches tracing on.
"""

# Lazy annotations.
from __future__ import annotations

# The real environment.
import os

# A monotonic clock for the cache and the deadline.
import time

# Types for the injectable parts.
from collections.abc import Callable, Mapping

# Real time: traces are stamped with it, even when the service runs on a frozen demo clock.
from datetime import UTC, datetime, timedelta

# Loose typing for the client.
from typing import Any

# The existing three-setting switch.
from setuhaul.langsmith_trace import tracing_status

# The same nearest-rank percentile as the recorded figures.
from setuhaul.model_report import percentile

# At most this many runs are read.
_LIMIT = 500
# One request may take this long, and the whole read not much longer.
_TIMEOUT_MS = 5000
_DEADLINE_S = 8.0
# A result is reused this long, to respect LangSmith's rate limits.
_CACHE_S = 60.0
# (key, project, endpoint) -> (when, result).
_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}


def _result(state: str, message: str = "", **values: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"state": state, "message": message, "runs": 0, "failed": 0, "p50_ms": None, "p95_ms": None,
                            "latest": None, "capped": False}
    return {**base, **values}


def _client(**kwargs: Any) -> Any:
    # Imported only when actually reading.
    from langsmith import Client
    from urllib3.util import Retry

    # No automatic retries: an admin can press the button again.
    return Client(retry_config=Retry(total=0), **kwargs)


def load_summary(
    env: Mapping[str, str] | None = None, client_factory: Callable[..., Any] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC), timer: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    e = os.environ if env is None else env
    status = tracing_status(e)
    # Off: say why, and contact nobody.
    if not status.enabled:
        return _result("off", status.reason)
    key, project, endpoint = e["LANGSMITH_API_KEY"], status.project or "", e.get("LANGSMITH_ENDPOINT", "")
    started = timer()
    cached = _cache.get((key, project, endpoint))
    if cached and started - cached[0] < _CACHE_S:
        return cached[1]
    try:
        extra = {"api_url": endpoint} if endpoint else {}
        client = (client_factory or _client)(api_key=key, timeout_ms=_TIMEOUT_MS, **extra)
        runs: list[Any] = []
        for run in client.list_runs(project_name=project, is_root=True, start_time=now() - timedelta(hours=24),
                                    select=["start_time", "end_time", "error", "status"], limit=_LIMIT):
            # Several pages can add up; give up rather than hang the request.
            if timer() - started > _DEADLINE_S:
                raise TimeoutError
            runs.append(run)
        times = [(r.end_time - r.start_time).total_seconds() * 1000 for r in runs if r.end_time and r.start_time]
        latest = max((r.start_time for r in runs if r.start_time), default=None)
        result = _result("ok", runs=len(runs), failed=sum(bool(r.error) for r in runs), p50_ms=percentile(times, 0.5),
                         p95_ms=percentile(times, 0.95), latest=latest.isoformat() if latest else None,
                         capped=len(runs) >= _LIMIT)
    # Only the kind of failure is shown: the text of an error can carry a key or an address.
    except Exception as error:  # noqa: BLE001 - an outside service must never break the screen
        result = _result("error", f"Could not read from LangSmith ({type(error).__name__}).")
    _cache[(key, project, endpoint)] = (started, result)
    return result
