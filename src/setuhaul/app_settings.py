"""Settings an admin may change from the screen, laid over the environment. Secrets are encrypted at rest."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken

# The master key's variable. It stays in the server's .env and is never editable or stored.
MASTER_KEY = "SETUHAUL_SETTINGS_KEY"

# What each block of the screen holds. These variable names, and no others, may be saved.
SECTIONS: dict[str, tuple[str, ...]] = {
    "model": ("SETUHAUL_MODEL_PROVIDER", "SETUHAUL_MODEL_ID", "SETUHAUL_MODEL_API_KEY", "SETUHAUL_OPENAI_REASONING_EFFORT",
              "SETUHAUL_MODEL_PRICE_IN_PER_MTOK", "SETUHAUL_MODEL_PRICE_OUT_PER_MTOK"),
    "langsmith": ("SETUHAUL_LANGSMITH_ENABLED", "SETUHAUL_LANGSMITH", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT",
                  "SETUHAUL_LANGSMITH_HIDE_TEXT"),
    "telegram": ("SETUHAUL_TELEGRAM_BOT_TOKEN", "SETUHAUL_TELEGRAM_WEBHOOK_SECRET"),
}
EDITABLE = frozenset(name for names in SECTIONS.values() for name in names)
SECRETS = frozenset({"SETUHAUL_MODEL_API_KEY", "LANGSMITH_API_KEY", "SETUHAUL_TELEGRAM_BOT_TOKEN",
                     "SETUHAUL_TELEGRAM_WEBHOOK_SECRET"})


@dataclass(frozen=True)
class Effective:
    """The settings in force: the environment with saved values laid over it."""

    env: dict[str, str]
    # For each editable name: "saved", "env" or "unset".
    source: dict[str, str]
    # Saved secrets that cannot be decrypted. Their value in env is empty, never the environment's.
    unreadable: frozenset[str]

    def __repr__(self) -> str:
        # env holds secrets: never print it.
        return "Effective()"


def _fernet(env: Mapping[str, str]) -> Fernet | None:
    try:
        return Fernet(env.get(MASTER_KEY, "").strip().encode())
    except ValueError:
        return None


def master_key_problem(env: Mapping[str, str]) -> str | None:
    if not env.get(MASTER_KEY, "").strip():
        return "missing"
    return None if _fernet(env) is not None else "invalid"


def save(conn: sqlite3.Connection, values: Mapping[str, str], *, env: Mapping[str, str], updated_by: str, now: datetime) -> None:
    """Store the values. The caller owns the transaction. Raises ValueError, having written nothing."""
    fernet = _fernet(env)
    rows = []
    for name, value in values.items():
        if name not in EDITABLE:
            raise ValueError(f"{name} is not an editable setting")
        if name in SECRETS:
            if fernet is None:
                raise ValueError(f"{MASTER_KEY} is {master_key_problem(env)}")
            rows.append((name, fernet.encrypt(value.encode()).decode(), 1, updated_by, now.isoformat()))
        else:
            rows.append((name, value, 0, updated_by, now.isoformat()))
    conn.executemany(
        "insert into app_settings (name, value, encrypted, updated_by, updated_at) values (?,?,?,?,?) "
        "on conflict(name) do update set value=excluded.value, encrypted=excluded.encrypted, "
        "updated_by=excluded.updated_by, updated_at=excluded.updated_at", rows)


def reset(conn: sqlite3.Connection, names: Iterable[str]) -> None:
    conn.executemany("delete from app_settings where name=?", [(n,) for n in names])


def effective(conn: sqlite3.Connection, env: Mapping[str, str]) -> Effective:
    try:
        rows = conn.execute("select name, value, encrypted from app_settings").fetchall()
    except sqlite3.OperationalError as error:
        # A database not yet migrated: the environment is all there is.
        if "no such table" not in str(error):
            raise
        rows = []
    merged = dict(env)
    source = {name: "env" if name in merged else "unset" for name in EDITABLE}
    unreadable: set[str] = set()
    fernet = _fernet(env)
    for name, value, encrypted in rows:
        # Anything else in the table is not a setting and never overrides the environment.
        if name not in EDITABLE:
            continue
        if encrypted:
            try:
                if fernet is None:
                    raise InvalidToken
                value = fernet.decrypt(value.encode()).decode()
            except (InvalidToken, UnicodeDecodeError):
                value = ""
                unreadable.add(name)
        merged[name] = value
        source[name] = "saved"
    return Effective(merged, source, frozenset(unreadable))
