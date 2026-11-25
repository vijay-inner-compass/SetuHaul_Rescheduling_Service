"""Same-origin check for cookie-authenticated writes.

A browser attaches the session cookie to requests that other sites trigger, so an unsafe method carrying that cookie
must come from our own pages. Machine callers (signed channel, Telegram webhook, bearer coordinators) send no session
cookie and are never looked at.
"""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from fastapi import Request

from setuhaul.auth import audit, sessions
from setuhaul.db import open_connection

if TYPE_CHECKING:
    from setuhaul.api import AppDeps

# Methods that can change something.
_UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Longest request path kept in the audit row.
_MAX_PATH = 256


def _host_port(netloc: str, scheme: str) -> str:
    # host[:port] in one form: lower case, and without the port that is the default for the scheme.
    default = {"http": ":80", "https": ":443"}.get(scheme, "")
    text = netloc.lower()
    return text.removesuffix(default) if default else text


def is_cross_site(request: Request, public_url: str = "") -> bool:
    # Only unsafe methods that carry the session cookie are judged.
    if request.method not in _UNSAFE or sessions.COOKIE_NAME not in request.cookies:
        return False
    # The browser's own verdict comes first: only our pages ("same-origin") or the user directly ("none").
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site not in ("same-origin", "none")
    # Otherwise compare the Origin's host with the one we are served as.
    origin = request.headers.get("origin")
    if origin is None:
        # Neither header: not a browser (browsers send Origin on cross-site writes).
        return False
    parts = urlsplit(origin)
    if public_url:
        public = urlsplit(public_url)
        expected = _host_port(public.netloc, public.scheme)
    else:
        expected = _host_port(request.headers.get("host", ""), parts.scheme)
    # "null" and other opaque origins have no host and so never match.
    return not parts.netloc or _host_port(parts.netloc, parts.scheme) != expected


def record_refusal(deps: AppDeps, request: Request) -> None:
    # Best effort: the request is refused whether or not the row can be written.
    path = "".join("?" if ord(c) < 32 or ord(c) == 127 else c for c in request.url.path[:_MAX_PATH])
    try:
        conn = open_connection(path=deps.db_path)
    except (sqlite3.Error, FileNotFoundError):
        return
    try:
        # Name the person whose cookie was used, when it is still a live session.
        user = sessions.resolve(conn, request.cookies.get(sessions.COOKIE_NAME, ""), deps.clock)
        audit.record(conn, actor_user_id=user.user_id if user else None, actor_email=user.email if user else None,
                     action="CROSS_SITE_REQUEST", target_type="path", target_id=path, outcome="FORBIDDEN",
                     ip=request.client.host if request.client else None, detail={"method": request.method}, clock=deps.clock)
        conn.commit()
    except sqlite3.Error:
        return
    finally:
        conn.close()
