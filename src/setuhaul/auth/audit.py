"""Append-only audit log. Secrets never reach the table."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

# json serialises the free-form detail; sqlite3 is the connection and row type.
import json
import sqlite3

from setuhaul.clock import Clock

# Keys whose values are credentials; matched case-insensitively and dropped before writing.
_FORBIDDEN_KEYS = frozenset({"password", "token", "code", "secret", "password_hash", "salt", "access_token", "id_token",
                            "otp", "pin", "secret_key", "authorization", "cookie"})


def scrub(value: object) -> object:
    # Drop forbidden keys at every depth so a nested payload cannot smuggle a secret in.
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if str(k).lower() not in _FORBIDDEN_KEYS}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def record(
    conn: sqlite3.Connection,
    *,
    actor_user_id: str | None,
    actor_email: str | None,
    action: str,
    target_type: str | None,
    target_id: str | None,
    outcome: str,
    ip: str | None,
    detail: dict[str, object] | None,
    clock: Clock,
) -> None:
    # Serialise the scrubbed detail, or store NULL when there is none.
    detail_json = json.dumps(scrub(detail), sort_keys=True) if detail is not None else None
    # Append one row stamped with the injected clock.
    conn.execute(
        "INSERT INTO audit_log(ts, actor_user_id, actor_email, action, target_type, target_id, outcome, ip, detail_json)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (clock.now().isoformat(), actor_user_id, actor_email, action, target_type, target_id, outcome, ip, detail_json),
    )


def query(
    conn: sqlite3.Connection,
    *,
    actor: str | None = None,
    action: str | None = None,
    outcome: str | None = None,
    since: str | None = None,
    limit: int = 200,
) -> list[sqlite3.Row]:
    # Build the filter list from whichever arguments were given.
    where: list[str] = []
    params: list[object] = []
    if actor is not None:
        # An actor filter matches either the user id or the email.
        where.append("(actor_user_id = ? OR actor_email = ? COLLATE NOCASE)")
        params += [actor, actor]
    if action is not None:
        where.append("action = ?")
        params.append(action)
    if outcome is not None:
        where.append("outcome = ?")
        params.append(outcome)
    if since is not None:
        where.append("ts >= ?")
        params.append(since)
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    # Newest first; audit_id breaks ties for rows written in the same instant.
    params.append(limit)
    cur = conn.execute(f"SELECT * FROM audit_log{clause} ORDER BY audit_id DESC LIMIT ?", params)
    return list(cur.fetchall())
