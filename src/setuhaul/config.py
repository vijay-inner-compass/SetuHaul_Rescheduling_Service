"""Runtime settings. Provisional values are pending Phase 0 approval (docs/decision-register.md)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# SHA-256 of the supplied SetuHaul_Data_Package/setuhaul_freight_operations.db
SEED_DB_SHA256 = "C02806E60827CA41C123D80E317F09ABB5585B870111F4C2CFE2177273F90700"


@dataclass(frozen=True)
class Settings:
    db_path: Path
    timezone: str = "Asia/Kolkata"
    redis_url: str = "redis://localhost:6379/0"
    hold_ttl_seconds: int = 180
    expiry_sweep_seconds: int = 30
    memory_max_turns: int = 20
    memory_max_chars: int = 12_000
    memory_ttl_seconds: int = 86_400
    option_set_retention_days: int = 30
    model_max_rounds: int = 6
    model_max_tool_calls: int = 4
    model_timeout_seconds: int = 8
    model_request_deadline_seconds: int = 30
    model_max_output_tokens: int = 800
    model_cost_alert_usd: float = 0.05
    rate_limit_per_minute: int = 20
    rate_limit_per_hour: int = 100
    sqlite_busy_timeout_ms: int = 5000
    cancel_cutoff_minutes: int = 120  # provisional (Q6): no automatic cancel closer than this to slot start
    notification_max_attempts: int = 3


def load_settings() -> Settings:
    raw = os.environ.get("SETUHAUL_DB_PATH", "data/setuhaul.sqlite3")
    path = Path(raw)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return Settings(
        db_path=path,
        timezone=os.environ.get("SETUHAUL_TIMEZONE", "Asia/Kolkata"),
        redis_url=os.environ.get("SETUHAUL_REDIS_URL", "redis://localhost:6379/0"),
    )
