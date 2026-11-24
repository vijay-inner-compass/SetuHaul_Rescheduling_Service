"""Server-side authorisation: the role guard and the facility scope. Only the session cookie is trusted."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

# sqlite3 for database trouble; Callable for the dependency type.
import sqlite3
from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import Request

from setuhaul.auth import audit, sessions
from setuhaul.auth.users import Role, User
from setuhaul.db import open_connection

if TYPE_CHECKING:
    from setuhaul.api import AppDeps


# A refusal with a stable code; the handler registered with the auth routes turns it into the API's error shape.
class AuthzError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def require_role(*roles: Role) -> Callable[[Request], User]:
    # The dependency FastAPI runs (in a worker thread, as it is a plain function) before the route body.
    def dependency(request: Request) -> User:
        # The app's deps (database path, clock) were stored on the app when the auth routes were registered.
        deps: AppDeps = request.app.state.deps
        # The cookie token is the only thing taken from the request; headers, query and body are never consulted.
        token = request.cookies.get(sessions.COOKIE_NAME)
        if not token:
            raise AuthzError(401, "UNAUTHENTICATED")
        try:
            conn = open_connection(path=deps.db_path)
        except (sqlite3.Error, FileNotFoundError):
            raise AuthzError(503, "DATABASE_UNAVAILABLE") from None
        try:
            # resolve re-reads the user, so a role change or disable applies on this very request.
            user = sessions.resolve(conn, token, deps.clock)
            if user is None:
                raise AuthzError(401, "UNAUTHENTICATED")
            if user.role not in roles:
                # Audit the refusal: the path and who asked, never the token or any request content.
                audit.record(conn, actor_user_id=user.user_id, actor_email=user.email, action="ACCESS_DENIED",
                             target_type="path", target_id=request.url.path, outcome="FORBIDDEN",
                             ip=request.client.host if request.client else None, detail={"role": user.role},
                             clock=deps.clock)
                conn.commit()
                raise AuthzError(403, "FORBIDDEN")
            return user
        except sqlite3.Error:
            raise AuthzError(503, "DATABASE_UNAVAILABLE") from None
        finally:
            conn.close()

    return dependency


def facility_ids(user: User) -> tuple[str, ...] | None:
    # None means every facility (admins); everyone else sees only the facilities they are bound to.
    return None if user.role == "ADMIN" else user.facilities


def assert_facility(user: User, facility_id: str) -> None:
    # Admins may touch any facility; others only their own.
    scope = facility_ids(user)
    if scope is not None and facility_id not in scope:
        raise AuthzError(403, "FORBIDDEN")
