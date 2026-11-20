"""Runtime settings. Provisional values are pending Phase 0 approval (docs/decision-register.md)."""

# Lazy annotations so type hints can refer to names defined later in the file.
from __future__ import annotations

# os reads environment variables (the only runtime configuration channel besides the .env file the scripts load).
import os

# dataclass gives Settings a constructor and, with frozen=True, makes it immutable once built.
from dataclasses import dataclass

# Path handles file locations portably across Windows and Linux.
from pathlib import Path

# The repository root: this file is src/setuhaul/config.py, so two levels up from its folder is the repo.
REPO_ROOT = Path(__file__).resolve().parents[2]
# SHA-256 of the supplied SetuHaul_Data_Package/setuhaul_freight_operations.db
# The audit script checks the seed database against this, so a changed source database is noticed.
SEED_DB_SHA256 = "C02806E60827CA41C123D80E317F09ABB5585B870111F4C2CFE2177273F90700"


# frozen=True: settings cannot be modified after creation, so no code can change a limit mid-run by accident.
@dataclass(frozen=True)
class Settings:
    # Where the working SQLite database lives. The only field without a default: every caller must say.
    db_path: Path
    # Operational timezone name. All business times (slots, ETAs, cut-offs) are in this zone.
    timezone: str = "Asia/Kolkata"
    # Where the short-term conversation memory and rate-limit counters live.
    redis_url: str = "redis://localhost:6379/0"
    # How long a slot hold lasts before it lapses (3 minutes): long enough to confirm, short enough not to block a dock.
    hold_ttl_seconds: int = 180
    # How often the background job sweeps for lapsed holds.
    expiry_sweep_seconds: int = 30
    # Conversation memory is bounded by turn count, character count and age, so it cannot grow without limit.
    memory_max_turns: int = 20
    memory_max_chars: int = 12_000
    memory_ttl_seconds: int = 86_400
    # Shown-option history is kept for 30 days for audit, then purged.
    option_set_retention_days: int = 30
    # Model-loop limits: at most 6 model rounds and 4 tool executions per driver message.
    model_max_rounds: int = 6
    model_max_tool_calls: int = 4
    # One provider call may take 8 s; the whole message (all rounds) may take 30 s.
    model_timeout_seconds: int = 8
    model_request_deadline_seconds: int = 30
    # Caps the length of each model answer, which also caps its cost.
    model_max_output_tokens: int = 800
    # Spend (USD) above which a message is flagged as expensive.
    model_cost_alert_usd: float = 0.05
    # Abuse limits per driver: 20 messages a minute and 100 an hour.
    rate_limit_per_minute: int = 20
    rate_limit_per_hour: int = 100
    # How long SQLite waits for a lock before reporting it busy (5 s), instead of failing instantly.
    sqlite_busy_timeout_ms: int = 5000
    cancel_cutoff_minutes: int = 120  # provisional (Q6): no automatic cancel closer than this to slot start
    # A downstream notification is retried up to 3 times before being marked failed.
    notification_max_attempts: int = 3
    escalation_sla_minutes: int | None = None  # None: no SLA approved, so none is ever promised (Q16)
    # A resolved case may be reopened within 24 hours.
    escalation_reopen_window_hours: int = 24
    model_cost_budget_usd: float = 0.05  # hard stop per handled message (alert level in the plan)


def load_settings() -> Settings:
    # Database location from the environment, with a default under the repo's data/ folder.
    raw = os.environ.get("SETUHAUL_DB_PATH", "data/setuhaul.sqlite3")
    path = Path(raw)
    # A relative path is anchored at the repo root, not the current directory, so scripts behave the same wherever run.
    if not path.is_absolute():
        path = REPO_ROOT / path
    # Only the three values that genuinely vary per deployment are read from the environment; the rest are fixed policy.
    return Settings(
        db_path=path,
        timezone=os.environ.get("SETUHAUL_TIMEZONE", "Asia/Kolkata"),
        redis_url=os.environ.get("SETUHAUL_REDIS_URL", "redis://localhost:6379/0"),
    )
