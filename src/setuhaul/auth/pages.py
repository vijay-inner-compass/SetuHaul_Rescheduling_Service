"""Server-rendered sign-in, admin and warehouse screens. Every dynamic value goes through esc(); scripts and styles are external."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import html
import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from setuhaul.api import claim_label
from setuhaul.auth import audit, onboarding, projects, sessions, users
from setuhaul.auth.admin_api import DEFAULT_AUDIT_LIMIT, parse_since
from setuhaul.auth.authz import AuthzError, assert_facility, require_role
from setuhaul.auth.users import User
from setuhaul.auth.warehouse_routes import (
    case_ids_in_scope,
    driver_chats_in_scope,
    driver_list,
    drivers_in_scope,
    mask_phone,
    partner_requests_in_scope,
)
from setuhaul.board import build_board
from setuhaul.db import open_connection
from setuhaul.escalation import get_case
from setuhaul.identity import AuthError

if TYPE_CHECKING:
    from setuhaul.api import AppDeps

# Where the external scripts and styles live, and the only names that may be served from there.
ASSET_DIR = Path(__file__).parent / "assets"
ASSET_TYPES = {"app.css": "text/css; charset=utf-8", "common.js": "text/javascript; charset=utf-8",
               "admin.js": "text/javascript; charset=utf-8", "warehouse.js": "text/javascript; charset=utf-8"}
# Sent with every HTML response: nothing but our own files may run or load, and nothing is cached.
SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
# Statuses a board cell can have, and so the only CSS class suffixes used for them.
_CELL_CLASSES = {"AVAILABLE": "available", "BOOKED": "booked", "HELD": "held", "BLOCKED": "blocked"}


def esc(value: object) -> str:
    # The one escaping helper: every dynamic value in every page passes through here, quotes included.
    return html.escape("" if value is None else str(value), quote=True)


def _segment(value: object) -> str:
    # An id placed in a URL path: percent-encoded, so it can never add a segment, a query or a fragment.
    return quote(str(value), safe="")


def _respond(body: str, status: int = 200) -> HTMLResponse:
    # An HTML response with the security headers.
    return HTMLResponse(body, status_code=status, headers=SECURITY_HEADERS)


def _redirect(to: str) -> RedirectResponse:
    # 303 so a form post or a reload never repeats a write.
    return RedirectResponse(to, status_code=303, headers=SECURITY_HEADERS)


def _layout(title: str, body: str, scripts: tuple[str, ...], user: User | None = None, nav: str = "") -> str:
    # The common frame: stylesheet, header with who is signed in, one status line the scripts write into, then the page.
    who = (f'<span class="who">{esc(user.email)} ({esc(user.role)})</span>'
           '<button type="button" data-api="/auth/logout" data-next="/">Sign out</button>') if user else ""
    tags = "".join(f'<script src="/app/assets/{esc(s)}" defer></script>' for s in scripts)
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{esc(title)}</title><link rel=\"stylesheet\" href=\"/app/assets/app.css\"></head><body>"
            f"<header><h1>SetuHaul</h1><nav>{nav}</nav><div class=\"session\">{who}</div></header>"
            f"<p id=\"status\" class=\"status\" role=\"alert\"></p><main>{body}</main>{tags}</body></html>")


def _table(headers: tuple[str, ...], rows: list[list[str]], empty: str = "Nothing to show.") -> str:
    # Cells arrive already escaped (or are markup this module built from escaped parts).
    if not rows:
        return f'<p class="empty">{esc(empty)}</p>'
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _button(label: str, api: str, body: dict[str, object] | None = None, confirm: str | None = None) -> str:
    # An action button: the script posts JSON to `api`, then reloads.
    data = f' data-body="{esc(json.dumps(body))}"' if body is not None else ""
    ask = f' data-confirm="{esc(confirm)}"' if confirm else ""
    return f'<button type="button" data-api="{esc(api)}"{data}{ask}>{esc(label)}</button>'


def _options(items: list[tuple[str, str]], selected: str | None = None, extra: dict[str, str] | None = None) -> str:
    # <option> list from (value, label) pairs; `extra` maps a value to one data-carrier attribute.
    out = []
    for value, label in items:
        mark = " selected" if value == selected else ""
        data = f' data-carrier="{esc(extra[value])}"' if extra and value in extra else ""
        out.append(f'<option value="{esc(value)}"{data}{mark}>{esc(label)}</option>')
    return "".join(out)


def _message_page(title: str, text: str, status: int, user: User | None = None) -> HTMLResponse:
    # A plain refusal or error page.
    body = f'<section><h2>{esc(title)}</h2><p>{esc(text)}</p><p><a href="/">Back</a></p></section>'
    return _respond(_layout(title, body, ("common.js",), user), status)


# ---- sign-in ----------------------------------------------------------------------------------------------
def _landing(local_admin: bool) -> str:
    # The sign-in page; the backup form exists only when the feature is switched on.
    backup = ""
    if local_admin:
        backup = ('<section class="backup"><h3>Backup sign-in</h3>'
                  '<form data-api="/auth/local-admin"><label>Email <input name="email" type="email" required autocomplete="username"></label>'
                  '<label>Password <input name="password" type="password" required autocomplete="current-password"></label>'
                  '<button type="submit">Sign in</button></form></section>')
    body = ('<section class="landing"><h2>Sign in</h2><p><a class="button" href="/auth/login">Sign in with Google</a></p>'
            f"{backup}</section>")
    return _layout("Sign in", body, ("common.js",))


# ---- admin screen -----------------------------------------------------------------------------------------
def _users_section(conn: sqlite3.Connection, me: User) -> str:
    facilities = [(r["facility_id"], f"{r['facility_id']} - {r['facility_name']}")
                  for r in conn.execute("select facility_id, facility_name from facilities order by facility_id")]
    rows = []
    # Every user, oldest first, each re-read through the users module like the JSON API does.
    for row in conn.execute("select user_id, last_login_at from users order by created_at, user_id").fetchall():
        found = users.find_by_id(conn, row["user_id"])
        if found is None:
            continue
        # Only an active user other than yourself can be disabled.
        action = (_button("Disable", f"/v1/admin/users/{_segment(found.user_id)}/disable", confirm=f"Disable {found.email}?")
                  if found.status == "ACTIVE" and found.user_id != me.user_id else "")
        rows.append([esc(found.email), esc(found.display_name), esc(found.role), esc(found.status),
                     esc(", ".join(found.facilities)), esc(row["last_login_at"] or "never"), action])
    invite = ('<h3>Invite a user</h3><form data-api="/v1/admin/users"><label>Email <input name="email" type="email" required></label>'
              '<label>Display name <input name="display_name" required maxlength="120"></label>'
              '<label>Role <select name="role"><option value="WAREHOUSE_AUTHORITY">Warehouse authority</option>'
              '<option value="ADMIN">Admin</option></select></label>'
              f'<label>Facilities <select name="facilities" multiple size="4">{_options(facilities)}</select></label>'
              '<button type="submit">Invite</button></form>')
    table = _table(("Email", "Name", "Role", "Status", "Facilities", "Last login", ""), rows)
    return f'<section id="users"><h2>Users</h2>{table}{invite}</section>'


def _pending_section(conn: sqlite3.Connection) -> str:
    rows = []
    for d in onboarding.pending(conn):
        # The licence number and the full phone are never shown.
        did = d["driver_id"]
        actions = (_button("Approve", f"/v1/drivers/{_segment(did)}/approve") + " "
                   + _button("Reject", f"/v1/drivers/{_segment(did)}/reject", confirm=f"Reject {d['driver_name']}?"))
        rows.append([esc(did), esc(d["driver_name"]), esc(mask_phone(d["phone"])), esc(d["carrier_id"]), esc(d["vehicle_id"]),
                     esc(d["licence_expiry"]), actions])
    table = _table(("Driver", "Name", "Phone", "Carrier", "Vehicle", "Licence expiry", ""), rows, "No drivers are waiting.")
    return f'<section id="pending"><h2>Pending drivers</h2>{table}</section>'


def _audit_section(conn: sqlite3.Connection, query: dict[str, str]) -> str:
    actor, action, outcome, since = (query.get(k, "").strip() for k in ("actor", "action", "outcome", "since"))
    note = ""
    floor = None
    # A bad timestamp is explained, and the log is then shown unfiltered by time.
    if since:
        try:
            floor = parse_since(since)
        except AuthError:
            note = '<p class="error">Since must be an ISO timestamp with an offset, for example 2026-08-04T09:00:00+05:30.</p>'
    found = audit.query(conn, actor=actor or None, action=action or None, outcome=outcome or None, since=floor,
                        limit=DEFAULT_AUDIT_LIMIT)
    rows = []
    for r in found:
        # Scrubbed again on the way out, then shown as plain escaped text.
        detail = json.dumps(audit.scrub(json.loads(r["detail_json"])), sort_keys=True) if r["detail_json"] else ""
        target = " ".join(x for x in (r["target_type"], r["target_id"]) if x)
        rows.append([esc(r["ts"]), esc(r["actor_email"] or r["actor_user_id"] or ""), esc(r["action"]), esc(target),
                     esc(r["outcome"]), f'<code>{esc(detail)}</code>'])
    form = ('<form method="get" action="/admin" class="filters">'
            f'<label>Actor <input name="actor" value="{esc(actor)}"></label>'
            f'<label>Action <input name="action" value="{esc(action)}"></label>'
            f'<label>Outcome <input name="outcome" value="{esc(outcome)}"></label>'
            f'<label>Since <input name="since" value="{esc(since)}" placeholder="2026-08-04T09:00:00+05:30"></label>'
            '<button type="submit">Filter</button></form>')
    table = _table(("Time", "Actor", "Action", "Target", "Outcome", "Detail"), rows, "No matching entries.")
    return f'<section id="audit"><h2>Audit log</h2>{form}{note}{table}</section>'


def render_admin(conn: sqlite3.Connection, me: User, query: dict[str, str]) -> str:
    # The admin screen: users, pending drivers, audit log.
    nav = '<a href="/admin">Admin</a> <a href="/warehouse">Warehouse</a>'
    body = _users_section(conn, me) + _pending_section(conn) + _audit_section(conn, query)
    return _layout("Admin", body, ("common.js", "admin.js"), me, nav)


# ---- warehouse screen -------------------------------------------------------------------------------------
def _board_section(board: dict[str, Any] | None, options: list[tuple[str, str]], selected: str | None) -> str:
    selector = ('<form method="get" action="/warehouse" class="filters"><label>Facility '
                f'<select name="facility">{_options(options, selected)}</select></label><button type="submit">Show</button></form>')
    if board is None:
        return f'<section id="board"><h2>Dock board</h2>{selector}<p class="empty">No facility to show.</p></section>'
    hours = board["hours"]
    head = "<th>Dock</th>" + "".join(f"<th>{esc(f'{h:02d}:00')}</th>" for h in hours)
    rows = []
    for dock in board["docks"]:
        cells = []
        for hour in hours:
            slots = [s for s in dock["slots"] if s["hour"] == hour]
            # One cell per hour; its class comes from a fixed whitelist, never from data.
            kind = _CELL_CLASSES.get(slots[0]["status"], "blocked") if slots else "none"
            past = " past" if slots and all(s["past"] for s in slots) else ""
            text = "; ".join(esc(s["label"] or s["status"].title()) for s in slots)
            cells.append(f'<td class="cell cell-{kind}{past}">{text}</td>')
        rows.append(f"<tr><th>{esc(dock['code'])} ({esc(dock['type'])})</th>{''.join(cells)}</tr>")
    table = f"<table class=\"board\"><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    pend = [[esc(p["request_ref"]), esc(p["order_reference"]), esc(p["driver"]), esc(f"{p['start']}-{p['end']}")]
            for p in board["pending_partner"]]
    waiting = _table(("Request", "Order", "Driver", "Proposed"), pend, "No requests are waiting for a partner.")
    return (f'<section id="board"><h2>Dock board</h2>{selector}<p class="legend">Facility {esc(board["facility"])}</p>{table}'
            f"<h3>Waiting for partner reply</h3>{waiting}</section>")


def _messages_section(conn: sqlite3.Connection, user: User) -> str:
    cases = [c for c in (get_case(conn, i) for i in case_ids_in_scope(conn, user, ("OPEN", "CLAIMED"))) if c is not None]
    case_rows = [[esc(c.case_id), esc(c.status), esc(c.reason_code), esc(c.summary), esc(c.shipment_id or ""),
                  esc(claim_label(conn, c.claimed_by) or "")] for c in cases]
    reqs = [[esc(r["request_ref"]), esc(r["facility_id"]), esc(r["shipment_id"]), esc(r["status"]), esc(r["email_status"]),
             esc(f"{r['proposed_start']} - {r['proposed_end']}")] for r in partner_requests_in_scope(conn, user)]
    chats = []
    for d in driver_chats_in_scope(conn, user):
        lines = "".join(f"<li><span class=\"role\">{esc(m['role'])}</span> {esc(m['text'])} <time>{esc(m['timestamp'])}</time></li>"
                        for m in d["messages"])
        chats.append(f"<div class=\"chat\"><h4>{esc(d['driver_name'])} ({esc(d['driver_id'])})</h4><ul>{lines}</ul></div>")
    note = ('<p class="note">Drivers on projects created by an administrator do not appear in a warehouse authority\'s chats; '
            "only drivers on projects you created are shown.</p>")
    return ('<section id="messages"><h2>Messages</h2><h3>Open cases</h3>'
            + _table(("Case", "Status", "Reason", "Summary", "Load", "Claimed by"), case_rows, "No open cases.")
            + "<h3>Partner slot requests</h3>"
            + _table(("Request", "Facility", "Load", "Status", "Email", "Proposed"), reqs, "No partner requests.")
            + f"<h3>Driver chats</h3>{note}" + ("".join(chats) or '<p class="empty">No chats to show.</p>') + "</section>")


def _projects_section(conn: sqlite3.Connection, user: User, today: date, facility_options: list[tuple[str, str]]) -> str:
    # Drivers who may be allocated: in the user's scope and eligible to work today.
    eligible = [(r["driver_id"], f"{r['driver_id']} - {r['driver_name']}") for r in drivers_in_scope(conn, user)
                if onboarding.driver_eligible(conn, r["driver_id"], today)]
    rows = []
    for p in projects.list_projects(conn, user):
        pid = str(p["project_id"])
        active = [str(d) for d in p["driver_ids"]]  # type: ignore[attr-defined]
        drivers = "".join(f"<div>{esc(d)} " + _button("Release", f"/v1/projects/{_segment(pid)}/release", {"driver_id": d},
                                                         f"Release {d}?") + "</div>" for d in active)
        allocate = ""
        free = [(v, label) for v, label in eligible if v not in active]
        if p["status"] == "ACTIVE" and free:
            allocate = (f'<form data-api="/v1/projects/{esc(_segment(pid))}/allocate"><select name="driver_id">{_options(free)}</select>'
                        '<button type="submit">Allocate</button></form>')
        rows.append([esc(pid), esc(p["customer_name"]), esc(p["contract_ref"]), esc(p["facility_id"]), esc(p["status"]),
                     esc(f"{p['starts_on']} - {p['ends_on'] or 'open'}"), drivers + allocate])
    create = ('<h3>New project</h3><form data-api="/v1/projects"><label>Customer <input name="customer_name" required maxlength="120"></label>'
              '<label>Contract reference <input name="contract_ref" required maxlength="64"></label>'
              f'<label>Facility <select name="facility_id">{_options(facility_options)}</select></label>'
              f'<label>Starts on <input name="starts_on" type="date" value="{esc(today.isoformat())}" required></label>'
              '<label>Ends on <input name="ends_on" type="date"></label><button type="submit">Create</button></form>')
    table = _table(("Project", "Customer", "Contract", "Facility", "Status", "Dates", "Drivers"), rows, "No projects yet.")
    return f'<section id="projects"><h2>Projects</h2>{table}{create}</section>'


def _drivers_section(conn: sqlite3.Connection, user: User, today: date) -> str:
    rows = []
    for d in driver_list(conn, user, today):
        revoke = (_button("Revoke Telegram", f"/v1/warehouse/drivers/{_segment(d['driver_id'])}/telegram/revoke",
                          confirm=f"Revoke Telegram for {d['driver_name']}?") if d["telegram_status"] == "BOUND" else "")
        rows.append([esc(d["driver_id"]), esc(d["driver_name"]), esc(d["phone"]), esc(d["approval_status"]),
                     "Yes" if d["licence_valid"] else "No", esc(d["telegram_status"]), revoke])
    carriers = [(r["carrier_id"], f"{r['carrier_id']} - {r['carrier_name']}")
                for r in conn.execute("select carrier_id, carrier_name from carriers where active_flag=1 order by carrier_id")]
    vehicles = conn.execute("select vehicle_id, carrier_id, registration_number from vehicles where active_flag=1 "
                            "order by carrier_id, vehicle_id").fetchall()
    vehicle_options = _options([(v["vehicle_id"], f"{v['vehicle_id']} - {v['registration_number']} ({v['carrier_id']})") for v in vehicles],
                               extra={v["vehicle_id"]: v["carrier_id"] for v in vehicles})
    add = ('<h3>Add a driver</h3><p class="note">New drivers wait for an administrator to approve them.</p>'
           '<form data-api="/v1/drivers"><label>Name <input name="name" required maxlength="100"></label>'
           '<label>Phone <input name="phone" required maxlength="32" autocomplete="off"></label>'
           '<label>Licence number <input name="licence_number" required maxlength="40" autocomplete="off"></label>'
           '<label>Licence expiry <input name="licence_expiry" type="date" required></label>'
           f'<label>Carrier <select name="carrier_id" id="carrier">{_options(carriers)}</select></label>'
           f'<label>Vehicle <select name="vehicle_id" id="vehicle">{vehicle_options}</select></label>'
           '<button type="submit">Add driver</button></form>')
    table = _table(("Driver", "Name", "Phone", "Approval", "Licence valid", "Telegram", ""), rows, "No drivers yet.")
    return f'<section id="drivers"><h2>Drivers</h2>{table}{add}</section>'


def render_warehouse(conn: sqlite3.Connection, user: User, now: Any, facility: str | None) -> HTMLResponse:
    # The facilities the user may pick from: an admin all of them, anyone else only their own.
    if user.role == "ADMIN":
        wanted = conn.execute("select facility_id, facility_name from facilities order by facility_id").fetchall()
    else:
        marks = ",".join("?" * len(user.facilities))
        wanted = conn.execute(f"select facility_id, facility_name from facilities where facility_id in ({marks}) order by facility_id",
                              user.facilities).fetchall()
    options = [(r["facility_id"], f"{r['facility_id']} - {r['facility_name']}") for r in wanted]
    chosen = facility or (options[0][0] if options else None)
    board = None
    if chosen is not None:
        # Same check as the board API: outside the user's scope looks exactly like a facility that does not exist.
        try:
            assert_facility(user, chosen)
        except AuthzError:
            return _message_page("Not found", "No such facility.", 404, user)
        board = build_board(conn, chosen, now)
        if board is None:
            return _message_page("Not found", "No such facility.", 404, user)
    today = now.date()
    body = (_board_section(board, options, chosen) + _messages_section(conn, user) + _projects_section(conn, user, today, options)
            + _drivers_section(conn, user, today))
    nav = '<a href="/warehouse">Warehouse</a>' + (' <a href="/admin">Admin</a>' if user.role == "ADMIN" else "")
    return _respond(_layout("Warehouse", body, ("common.js", "warehouse.js"), user, nav))


# ---- routes -----------------------------------------------------------------------------------------------
def register_pages(app: FastAPI, deps: AppDeps) -> None:
    # Run a page builder on a read-only connection; database trouble becomes a plain 503 page.
    def read_page(user: User | None, build: Any) -> Response:
        try:
            conn = open_connection(path=deps.db_path, readonly=True)
        except (sqlite3.Error, FileNotFoundError):
            return _message_page("Unavailable", "The database is not available.", 503, user)
        try:
            return build(conn)  # type: ignore[no-any-return]
        except sqlite3.Error:
            return _message_page("Unavailable", "The database is not available.", 503, user)
        finally:
            conn.close()

    # The user behind the session cookie, or None (also when the database cannot say).
    def signed_in(request: Request) -> User | None:
        token = request.cookies.get(sessions.COOKIE_NAME)
        if not token:
            return None
        try:
            conn = open_connection(path=deps.db_path, readonly=True)
        except (sqlite3.Error, FileNotFoundError):
            return None
        try:
            return sessions.resolve(conn, token, deps.clock)
        except sqlite3.Error:
            return None
        finally:
            conn.close()

    # The HTML-aware front of the role guard: the JSON guard runs unchanged, only its refusal is shown as a page.
    def guarded(request: Request, *roles: Any) -> User | Response:
        try:
            return require_role(*roles)(request)
        except AuthzError as exc:
            # Anonymous (or an expired session) goes to the sign-in page.
            if exc.status == 401:
                return _redirect("/")
            if exc.status == 403:
                # Show who is signed in, with the sign-out button, like every other page.
                return _message_page("Not allowed", "Your account cannot open this page.", 403, signed_in(request))
            return _message_page("Unavailable", "The service is not available.", exc.status)

    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request) -> Response:
        # A signed-in user goes straight to their screen.
        token = request.cookies.get(sessions.COOKIE_NAME)
        if token:
            try:
                conn = open_connection(path=deps.db_path, readonly=True)
            except (sqlite3.Error, FileNotFoundError):
                return _message_page("Unavailable", "The database is not available.", 503)
            try:
                user = sessions.resolve(conn, token, deps.clock)
            except sqlite3.Error:
                return _message_page("Unavailable", "The database is not available.", 503)
            finally:
                conn.close()
            if user is not None:
                return _redirect("/admin" if user.role == "ADMIN" else "/warehouse")
        return _respond(_landing(deps.local_admin_enabled))

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(request: Request) -> Response:
        user = guarded(request, "ADMIN")
        if isinstance(user, Response):
            return user
        query = {k: v for k, v in request.query_params.items()}
        return read_page(user, lambda conn: _respond(render_admin(conn, user, query)))

    @app.get("/warehouse", response_class=HTMLResponse)
    def warehouse_page(request: Request) -> Response:
        user = guarded(request, "WAREHOUSE_AUTHORITY", "ADMIN")
        if isinstance(user, Response):
            return user
        facility = request.query_params.get("facility") or None
        return read_page(user, lambda conn: render_warehouse(conn, user, deps.clock.now(), facility))

    @app.get("/app/assets/{name}")
    def asset(name: str) -> Response:
        # Only the four known files, by exact name: nothing the caller types ever becomes a path.
        kind = ASSET_TYPES.get(name)
        if kind is None:
            return Response("Not found", status_code=404, media_type="text/plain",
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})
        return Response((ASSET_DIR / name).read_bytes(), media_type=kind,
                        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})
