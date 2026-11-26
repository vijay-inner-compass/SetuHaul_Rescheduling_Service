"""Figures for the admin Model tab, read from the recorded calls. Read only."""

# Lazy annotations.
from __future__ import annotations

# Rounding up for the nearest-rank percentile.
import math

# Connection and row types.
import sqlite3

# Value types for the totals.
from dataclasses import dataclass


# Nearest rank: the smallest value with at least a fraction p of the values at or below it.
def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(math.ceil(p * len(ordered)), 1) - 1]


@dataclass(frozen=True)
class CallTotals:
    calls: int
    failed: int
    p50_ms: float | None
    p95_ms: float | None
    input_tokens: int
    output_tokens: int
    # None when no call has a price.
    cost_usd: float | None
    # Successful calls with no price: the cost shown is then incomplete.
    unpriced: int


@dataclass(frozen=True)
class MessageTotals:
    messages: int
    p50_ms: float | None
    p95_ms: float | None
    avg_calls: float
    avg_input_tokens: float
    avg_output_tokens: float
    avg_cost_usd: float | None


# Totals over the calls since a time (None means all of them).
def call_totals(conn: sqlite3.Connection, since_epoch: float | None) -> CallTotals:
    rows = conn.execute("select duration_ms, outcome, input_tokens, output_tokens, cost_usd from model_calls "
                        "where ts_epoch >= ?", (since_epoch if since_epoch is not None else float("-inf"),)).fetchall()
    times = [r["duration_ms"] for r in rows]
    costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    return CallTotals(
        calls=len(rows), failed=sum(r["outcome"] != "OK" for r in rows),
        p50_ms=percentile(times, 0.5), p95_ms=percentile(times, 0.95),
        input_tokens=sum(r["input_tokens"] for r in rows), output_tokens=sum(r["output_tokens"] for r in rows),
        cost_usd=sum(costs) if costs else None,
        unpriced=sum(r["outcome"] == "OK" and r["cost_usd"] is None for r in rows))


# One row per message with its calls added up; shared by the averages and the recent list.
_TURNS = ("select t.turn_id, t.ts, t.ts_epoch, t.duration_ms, t.status, t.reason_code, count(c.call_id) as calls, "
          "coalesce(sum(c.input_tokens), 0) as input_tokens, coalesce(sum(c.output_tokens), 0) as output_tokens, "
          "sum(c.cost_usd) as cost_usd from model_turns t left join model_calls c on c.turn_id = t.turn_id group by t.turn_id")


def message_totals(conn: sqlite3.Connection) -> MessageTotals:
    rows = conn.execute(_TURNS).fetchall()
    n = len(rows)
    times = [r["duration_ms"] for r in rows]
    costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]

    def average(key: str) -> float:
        return float(sum(r[key] for r in rows)) / n if n else 0.0

    return MessageTotals(n, percentile(times, 0.5), percentile(times, 0.95), average("calls"), average("input_tokens"),
                         average("output_tokens"), sum(costs) / len(costs) if costs else None)


def recent_turns(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(_TURNS + " order by t.ts_epoch desc, t.rowid desc limit ?", (limit,)).fetchall()


def recent_calls(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute("select * from model_calls order by ts_epoch desc, rowid desc limit ?", (limit,)).fetchall()
