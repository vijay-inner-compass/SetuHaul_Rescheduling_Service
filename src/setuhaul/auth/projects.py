"""Customer-contract projects and the drivers allocated to them.

A project belongs to one facility. A warehouse authority creates projects in facilities they are bound to and
allocates eligible drivers to their own projects; an admin may do either anywhere. A project outside the caller's
facilities looks like it does not exist.
"""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

# re checks the contract reference; secrets: unguessable short ids; sqlite3 for the connection and the unique backstop.
import re
import secrets
import sqlite3
from datetime import date

from setuhaul.auth import audit
from setuhaul.auth.authz import AuthzError, assert_facility, facility_clause
from setuhaul.auth.onboarding import driver_eligible
from setuhaul.auth.refusals import InvalidRequest
from setuhaul.auth.users import User
from setuhaul.clock import Clock
from setuhaul.db import IST, immediate
from setuhaul.identity import AuthError

# Field bounds.
_MAX_NAME = 120
# A contract reference: starts with a letter or digit, then up to 63 of letters, digits and . _ / -
_CONTRACT_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,63}")
# Project dates outside this window are typing mistakes.
_FIRST_DATE, _LAST_DATE = date(2000, 1, 1), date(2100, 12, 31)
# Longest caller-supplied id kept in a probe audit row.
_MAX_TARGET = 64


def _has_control(text: str) -> bool:
    # Control characters (including NUL and DEL) never belong in a name or reference.
    return any(ord(c) < 32 or ord(c) == 127 for c in text)


def _new_id() -> str:
    return "PRJ-" + secrets.token_hex(4).upper()


def _refuse(conn: sqlite3.Connection, actor: User, action: str, target_type: str, target_id: str | None,
            clock: Clock) -> AuthError:
    # An audited refusal, committed before the error leaves; the caller raises the returned error.
    audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action=action, target_type=target_type,
                 target_id=target_id, outcome="FORBIDDEN", ip=None, detail={"role": actor.role}, clock=clock)
    conn.commit()
    return AuthError("FORBIDDEN")


class _Refused(Exception):
    """A refusal found inside a transaction: leaving the block rolls it back, then the audit row is written."""

    def __init__(self, code: str, action: str, outcome: str, target_id: str) -> None:
        super().__init__(code)
        self.code, self.action, self.outcome, self.target_id = code, action, outcome, target_id


def _audit_refusal(conn: sqlite3.Connection, actor: User, refused: _Refused, clock: Clock) -> AuthError:
    # Written after the rolled-back transaction, committed before the error leaves; the caller raises the result.
    audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action=refused.action, target_type="project",
                 target_id=refused.target_id, outcome=refused.outcome, ip=None, detail={"role": actor.role}, clock=clock)
    conn.commit()
    return AuthError(refused.code)


def _check_role(conn: sqlite3.Connection, actor: User, action: str, target_id: str | None, clock: Clock) -> None:
    # Only active admins and warehouse authorities may manage projects.
    if actor.status != "ACTIVE" or actor.role not in ("ADMIN", "WAREHOUSE_AUTHORITY"):
        raise _refuse(conn, actor, action, "project", target_id, clock)


def create(
    conn: sqlite3.Connection,
    *,
    actor: User,
    customer_name: str,
    contract_ref: str,
    facility_id: str,
    starts_on: date,
    ends_on: date | None,
    clock: Clock,
) -> str:
    # Role first, then the facility scope (a warehouse authority only in facilities they are bound to).
    _check_role(conn, actor, "PROJECT_CREATED", None, clock)
    try:
        assert_facility(actor, facility_id)
    except AuthzError:
        # Audited like every other refusal, then re-raised unchanged.
        audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action="PROJECT_CREATED",
                     target_type="facility", target_id=facility_id, outcome="FORBIDDEN", ip=None,
                     detail={"role": actor.role}, clock=clock)
        conn.commit()
        raise
    # Sane text fields: non-empty, bounded, no control characters.
    name, ref = customer_name.strip(), contract_ref.strip()
    if not name or len(name) > _MAX_NAME or _has_control(name):
        raise InvalidRequest(("customer_name", f"must be 1 to {_MAX_NAME} characters of ordinary text"))
    if _CONTRACT_REF.fullmatch(ref) is None:
        raise InvalidRequest(("contract_ref", "is not in the expected format"))
    # Both dates must be plausible.
    for field, day in (("starts_on", starts_on), ("ends_on", ends_on)):
        if day is not None and not _FIRST_DATE <= day <= _LAST_DATE:
            raise InvalidRequest((field, f"must be between {_FIRST_DATE.year} and {_LAST_DATE.year}"))
    # A contract cannot end before it starts.
    if ends_on is not None and ends_on < starts_on:
        raise InvalidRequest(("ends_on", "is before the start date"))
    pid = _new_id()
    # One transaction: the existence checks, the row and the audit row stand or fall together.
    with immediate(conn):
        if conn.execute("select 1 from facilities where facility_id=?", (facility_id,)).fetchone() is None:
            raise AuthError("FACILITY_NOT_FOUND")
        # Compared lower-cased, matching the unique index on lower(contract_ref).
        if conn.execute("select 1 from projects where lower(contract_ref)=lower(?)", (ref,)).fetchone() is not None:
            raise AuthError("DUPLICATE_CONTRACT")
        try:
            conn.execute(
                "insert into projects(project_id, customer_name, contract_ref, facility_id, status, starts_on, ends_on,"
                " created_by) values (?,?,?,?,'ACTIVE',?,?,?)",
                (pid, name, ref, facility_id, starts_on.isoformat(), ends_on.isoformat() if ends_on else None,
                 actor.user_id))
        except sqlite3.IntegrityError:
            # The UNIQUE constraint is the backstop for a race the check above lost.
            raise AuthError("DUPLICATE_CONTRACT") from None
        # Ids only: the customer name stays out of the trail.
        audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action="PROJECT_CREATED",
                     target_type="project", target_id=pid, outcome="OK", ip=None,
                     detail={"facility_id": facility_id}, clock=clock)
    return pid


def _load_for_change(conn: sqlite3.Connection, actor: User, project_id: str, action: str) -> sqlite3.Row:
    # A project outside the actor's facilities is "not found", so its existence is never revealed.
    clause, params = facility_clause(actor, "facility_id")
    row = conn.execute(f"select * from projects where project_id=? and {clause}", (project_id, *params)).fetchone()
    if row is None:
        # It exists but is out of scope: the answer is the same as for a missing id, and the probe is audited.
        if conn.execute("select 1 from projects where project_id=?", (project_id,)).fetchone() is not None:
            raise _Refused("PROJECT_NOT_FOUND", "PROJECT_PROBE", "NOT_FOUND", project_id[:_MAX_TARGET])
        raise AuthError("PROJECT_NOT_FOUND")
    # Visible but not theirs: only the creator or an admin may change the allocation.
    if actor.role != "ADMIN" and row["created_by"] != actor.user_id:
        raise _Refused("FORBIDDEN", action, "FORBIDDEN", project_id)
    return row  # type: ignore[no-any-return]


def allocate(conn: sqlite3.Connection, *, actor: User, project_id: str, driver_id: str, clock: Clock) -> None:
    _check_role(conn, actor, "DRIVER_ALLOCATED", project_id, clock)
    # Eligibility and the project's end are judged on today's date in IST, the same rule the bot uses.
    today = clock.now().astimezone(IST).date()
    # One immediate transaction: the checks and the write see the same state, so two requests cannot both pass.
    try:
        with immediate(conn):
            project = _load_for_change(conn, actor, project_id, "DRIVER_ALLOCATED")
            if project["status"] != "ACTIVE":
                raise AuthError("PROJECT_CLOSED")
            # The last day still counts; after it nobody new joins the project.
            if project["ends_on"] and date.fromisoformat(project["ends_on"]) < today:
                raise AuthError("PROJECT_ENDED")
            # Unknown, inactive, pending, rejected and licence-expired drivers are all the same refusal.
            if not driver_eligible(conn, driver_id, today):
                raise AuthError("DRIVER_NOT_ELIGIBLE")
            existing = conn.execute("select released_at from project_drivers where project_id=? and driver_id=?",
                                    (project_id, driver_id)).fetchone()
            now = clock.now().isoformat()
            if existing is None:
                conn.execute("insert into project_drivers(project_id, driver_id, allocated_by, allocated_at)"
                             " values (?,?,?,?)", (project_id, driver_id, actor.user_id, now))
            elif existing["released_at"] is None:
                raise AuthError("ALREADY_ALLOCATED")
            else:
                # Re-allocation after release reuses the row (the key is project+driver); the audit trail keeps the history.
                conn.execute("update project_drivers set allocated_by=?, allocated_at=?, released_at=null"
                             " where project_id=? and driver_id=?", (actor.user_id, now, project_id, driver_id))
            audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action="DRIVER_ALLOCATED",
                         target_type="driver", target_id=driver_id, outcome="OK", ip=None,
                         detail={"project_id": project_id}, clock=clock)
    except _Refused as refused:
        raise _audit_refusal(conn, actor, refused, clock) from None


def release(conn: sqlite3.Connection, *, actor: User, project_id: str, driver_id: str, clock: Clock) -> None:
    _check_role(conn, actor, "DRIVER_RELEASED", project_id, clock)
    try:
        with immediate(conn):
            # Same scope and ownership rule as allocation; a closed project may still be tidied up.
            _load_for_change(conn, actor, project_id, "DRIVER_RELEASED")
            cur = conn.execute("update project_drivers set released_at=? where project_id=? and driver_id=?"
                               " and released_at is null", (clock.now().isoformat(), project_id, driver_id))
            # Only an active allocation can be released.
            if cur.rowcount != 1:
                raise AuthError("NOT_ALLOCATED")
            audit.record(conn, actor_user_id=actor.user_id, actor_email=actor.email, action="DRIVER_RELEASED",
                         target_type="driver", target_id=driver_id, outcome="OK", ip=None,
                         detail={"project_id": project_id}, clock=clock)
    except _Refused as refused:
        raise _audit_refusal(conn, actor, refused, clock) from None


def drivers_of(conn: sqlite3.Connection, project_id: str) -> list[str]:
    # Active allocations only, in a stable order.
    return [r["driver_id"] for r in conn.execute(
        "select driver_id from project_drivers where project_id=? and released_at is null order by driver_id",
        (project_id,))]


def _live_allocations(conn: sqlite3.Connection, user: User, today: date) -> list[sqlite3.Row]:
    # Active allocations on ACTIVE projects not past their end date (IST today; the last day still counts).
    clause, params = facility_clause(user, "p.facility_id")
    # Admins are not limited by creator; everyone else only by the projects they created.
    owner, owner_params = ("1=1", ()) if user.role == "ADMIN" else ("p.created_by=?", (user.user_id,))
    return conn.execute(
        "select pd.driver_id, pd.allocated_at from project_drivers pd join projects p on p.project_id=pd.project_id"
        f" where pd.released_at is null and p.status='ACTIVE' and (p.ends_on is null or p.ends_on>=?) and {owner} and {clause}",
        (today.isoformat(), *owner_params, *params)).fetchall()


def visible_driver_ids(conn: sqlite3.Connection, user: User, today: date) -> set[str]:
    """Drivers the user may see through projects: actively allocated to an ACTIVE project not past its end date.

    An admin sees all of them. A warehouse authority sees only those on projects they created and whose facility
    they are still bound to; with no facilities that is the empty set. Released drivers, closed projects and
    projects whose ends_on is before `today` (IST) drop out.
    """
    return {r["driver_id"] for r in _live_allocations(conn, user, today)}


def list_projects(conn: sqlite3.Connection, user: User) -> list[dict[str, object]]:
    # Projects in the user's facilities (all for an admin), each with its active drivers.
    clause, params = facility_clause(user, "facility_id")
    rows = conn.execute(f"select * from projects where {clause} order by project_id", params).fetchall()
    return [{**dict(r), "driver_ids": drivers_of(conn, r["project_id"])} for r in rows]
