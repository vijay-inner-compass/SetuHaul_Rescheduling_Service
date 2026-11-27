"""Server-rendered sign-in, admin and warehouse screens. Every dynamic value goes through esc(); scripts and styles are external."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

import html
import json
import sqlite3
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from setuhaul.api import WEB_PREFIX, claim_label, env_in_force, model_in_force
from setuhaul.auth import audit, onboarding, projects, sessions, telegram, users
from setuhaul.auth.admin_api import DEFAULT_AUDIT_LIMIT, parse_since
from setuhaul.auth.authz import AuthzError, assert_facility, require_role
from setuhaul.auth.facility_routes import pending_changes_in_scope
from setuhaul.auth.settings_routes import settings_view
from setuhaul.auth.signin_notice import NOTICES
from setuhaul.auth.users import User
from setuhaul.auth.warehouse_routes import (
    MAX_LIMIT,
    case_ids_in_scope,
    driver_chats_in_scope,
    driver_list,
    drivers_in_scope,
    mask_phone,
    partner_requests_in_scope,
)
from setuhaul.board import build_board
from setuhaul.config import load_settings
from setuhaul.db import IST, open_connection
from setuhaul.escalation import CaseView, case_events, get_case
from setuhaul.identity import AuthError
from setuhaul.labels import moment as when
from setuhaul.labels import reason as why
from setuhaul.labels import word
from setuhaul.langsmith_trace import tracing_status
from setuhaul.model_gateway import ModelGateway, ModelPolicy
from setuhaul.model_report import (
    CallTotals,
    call_totals,
    message_totals,
    recent_calls,
    recent_turns,
)
from setuhaul.model_usage import describe_model

if TYPE_CHECKING:
    from setuhaul.api import AppDeps

# Where the external scripts and styles live, and the only names that may be served from there.
ASSET_DIR = Path(__file__).parent / "assets"
ASSET_TYPES = {"tokens.css": "text/css; charset=utf-8", "app.css": "text/css; charset=utf-8", "common.js": "text/javascript; charset=utf-8",
               "admin.js": "text/javascript; charset=utf-8", "tabs.js": "text/javascript; charset=utf-8",
               "model.js": "text/javascript; charset=utf-8",
               "settings.js": "text/javascript; charset=utf-8",
               "warehouse.js": "text/javascript; charset=utf-8", "exceptions.js": "text/javascript; charset=utf-8",
               "gate.js": "text/javascript; charset=utf-8"}
# Sent with every HTML response: nothing but our own files may run or load, and nothing is cached.
SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
# Which facility the warehouse screen was last showing. Not a permission: scope is checked on every request.
FACILITY_COOKIE = "sh_facility"
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
    who = (f'<span class="who">{esc(user.email)} ({esc(word(user.role))})</span>'
           '<button type="button" data-api="/auth/logout" data-next="/">Sign out</button>') if user else ""
    tags = "".join(f'<script src="/app/assets/{esc(s)}" defer></script>' for s in scripts)
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{esc(title)}</title><link rel=\"stylesheet\" href=\"/app/assets/tokens.css\">"
            "<link rel=\"stylesheet\" href=\"/app/assets/app.css\"></head><body>"
            f"<header><h1>SetuHaul</h1><nav>{nav}</nav><div class=\"session\">{who}</div></header>"
            f"<p id=\"status\" class=\"status\" role=\"alert\"></p><main>{body}</main>{tags}</body></html>")


def _table(headers: tuple[str, ...], rows: list[list[str]], empty: str = "Nothing to show.") -> str:
    # Cells arrive already escaped (or are markup this module built from escaped parts).
    if not rows:
        return f'<p class="empty">{esc(empty)}</p>'
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    # A wide table scrolls inside its own box, so it never widens the page on a narrow screen.
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _button(label: str, api: str, body: dict[str, object] | None = None, confirm: str | None = None,
            done: str | None = None, key: str | None = None) -> str:
    # An action button: the script posts JSON to `api`, then reloads. `done` is what it says afterwards; `key` lets the
    # script find the same button again after a refresh.
    data = f' data-body="{esc(json.dumps(body))}"' if body is not None else ""
    ask = f' data-confirm="{esc(confirm)}"' if confirm else ""
    after = f' data-done="{esc(done)}"' if done else ""
    mark = f' data-key="{esc(key)}"' if key else ""
    return f'<button type="button" data-api="{esc(api)}"{data}{ask}{after}{mark}>{esc(label)}</button>'


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
def _dock_drawing() -> str:
    # Six numbered bay doors, as on the dock board: one is open with a truck backed in. Presentation only, no data.
    bays = []
    for n in range(1, 7):
        x = 8 + (n - 1) * 64
        slats = "".join(f'<line class="slat" x1="{x + 4}" y1="{y}" x2="{x + 48}" y2="{y}"/>' for y in range(52, 125, 12))
        door = f'<rect class="opening" x="{x}" y="40" width="52" height="92"/>' if n == 4 else \
               f'<rect class="door" x="{x}" y="40" width="52" height="92"/>{slats}'
        bays.append(f'{door}<text x="{x + 26}" y="28">D{n}</text>'
                    f'<rect class="bumper" x="{x - 2}" y="132" width="8" height="12"/><rect class="bumper" x="{x + 46}" y="132" width="8" height="12"/>')
    truck = ('<rect class="trailer" x="204" y="68" width="44" height="64"/><line class="seam" x1="226" y1="68" x2="226" y2="132"/>'
             '<rect class="wheel" x="206" y="132" width="10" height="14"/><rect class="wheel" x="236" y="132" width="10" height="14"/>')
    return (f'<svg class="docks" viewBox="0 0 392 160" aria-hidden="true" focusable="false">{"".join(bays)}{truck}'
            '<line class="yard" x1="0" y1="146" x2="392" y2="146"/></svg>')


# Google's "G" mark, drawn inline so nothing is fetched from another site.
_GOOGLE_MARK = (
    '<svg class="mark" viewBox="0 0 18 18" aria-hidden="true" focusable="false">'
    '<path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.92c1.7-1.57 2.68-3.88 2.68-6.62z"/>'
    '<path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.92-2.26c-.8.54-1.84.86-3.04.86-2.34 0-4.33-1.58-5.04-3.71H.96v2.33A9 9 0 0 0 9 18z"/>'
    '<path fill="#FBBC05" d="M3.96 10.71A5.41 5.41 0 0 1 3.68 9c0-.59.1-1.17.28-1.71V4.96H.96A9 9 0 0 0 0 9c0 1.45.35 2.83.96 4.04l3-2.33z"/>'
    '<path fill="#EA4335" d="M9 3.58c1.32 0 2.5.45 3.44 1.35l2.58-2.58C13.46.89 11.43 0 9 0A9 9 0 0 0 .96 4.96l3 2.33C4.67 5.16 6.66 3.58 9 3.58z"/>'
    '</svg>')


def _landing(google: bool, local_admin: bool, notice: str = "") -> str:
    # The sign-in page. Only what is set up is offered: a button that can only fail is worse than none.
    button = f'<a class="google" href="/auth/login">{_GOOGLE_MARK}<span>Sign in with Google</span></a>' if google else ""
    backup = ""
    if local_admin:
        # Folded away when Google is the normal way in; open when the password is the only way in.
        state = "" if google else " open"
        backup = (f'<details class="backup"{state}><summary>Administrator password</summary>'
                  '<form data-api="/auth/local-admin">'
                  '<label for="email">Email</label><input id="email" name="email" type="email" required autocomplete="username">'
                  '<label for="password">Password</label><span class="secret">'
                  '<input id="password" name="password" type="password" required autocomplete="current-password">'
                  '<button type="button" data-reveal="password" aria-pressed="false">Show</button></span>'
                  '<button type="submit">Sign in</button></form></details>')
    if not google and not local_admin:
        button = ('<p class="empty">No sign-in is configured. Set SETUHAUL_GOOGLE_CLIENT_ID and SETUHAUL_GOOGLE_CLIENT_SECRET (with '
                  'SETUHAUL_SESSION_SECRET), or SETUHAUL_LOCAL_ADMIN=1 with SETUHAUL_BOOTSTRAP_ADMIN_EMAIL, then restart.</p>')
    lead = "For administrators and warehouse teams." if google else "Administrator access."
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>Sign in</title><link rel=\"stylesheet\" href=\"/app/assets/tokens.css\">"
            "<link rel=\"stylesheet\" href=\"/app/assets/app.css\"></head><body class=\"login\">"
            '<div class="brand"><p class="wordmark">SetuHaul</p>'
            '<div class="pitch"><h1>Dock scheduling and rescheduling</h1>'
            "<p>One place for warehouse teams to see bay bookings, driver messages and changes as they happen.</p></div>"
            f"{_dock_drawing()}</div>"
            f'<main class="signin"><div class="panel"><h2>Sign in</h2><p class="lead">{lead}</p>'
            f'<p id="status" class="status" role="alert">{esc(notice)}</p>{button}{backup}'
            '<p class="fine">Access is by invitation, and sign-ins are recorded. Drivers use Telegram and do not sign in here.</p>'
            '</div></main><script src="/app/assets/common.js" defer></script></body></html>')


def _checkboxes(name: str, items: list[tuple[str, str]], chosen: tuple[str, ...] = ()) -> str:
    return "".join(f'<label class="check"><input type="checkbox" name="{name}" value="{esc(value)}"'
                   f'{" checked" if value in chosen else ""}> {esc(label)}</label>' for value, label in items)


def _tabs(items: tuple[tuple[str, str], ...], counts: Mapping[str, int] | None = None) -> str:
    # The section switcher shared by the warehouse and admin screens; tabs.js does the switching. A tab with a queue
    # behind it carries the number waiting; the span stays when empty so the live refresh can fill it.
    def badge(key: str) -> str:
        if counts is None or key not in counts:
            return ""
        return f' <span class="count {key}-count">{counts[key] or ""}</span>'

    links = "".join(f'<a href="#{key}" data-tab="{key}">{esc(label)}{badge(key)}</a>' for key, label in items)
    return f'<nav class="tabs" aria-label="Sections">{links}</nav>'


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
        base = f"/v1/admin/users/{_segment(found.user_id)}"
        action = (_button("Disable", f"{base}/disable", confirm=f"Disable {found.email}? Their sessions end at once.")
                  if found.status == "ACTIVE" and found.user_id != me.user_id else "")
        if found.status == "DISABLED":
            action = _button("Re-enable", f"{base}/enable", confirm=f"Let {found.email} sign in again?", done="User re-enabled.")
        if found.role in users.FACILITY_BOUND:
            action += (f'<details><summary>Change facilities</summary><form data-api="{esc(base)}/facilities" '
                       f'data-done="Facilities saved.">{_checkboxes("facilities", facilities, found.facilities)}'
                       '<button type="submit">Save facilities</button></form></details>')
        rows.append([esc(found.email), esc(found.display_name), esc(word(found.role)), esc(word(found.status)),
                     esc(", ".join(found.facilities)), esc(when(row["last_login_at"])), action])
    invite = ('<h3>Invite a user</h3><form data-api="/v1/admin/users"><label>Email <input name="email" type="email" required></label>'
              '<label>Display name <input name="display_name" required maxlength="120"></label>'
              '<label>Role <select name="role"><option value="WAREHOUSE_AUTHORITY">Warehouse authority</option>'
              '<option value="SECURITY">Security (gate)</option><option value="ADMIN">Admin</option></select></label>'
              f'<fieldset data-facilities><legend>Facilities</legend>{_checkboxes("facilities", facilities)}</fieldset>'
              '<button type="submit">Invite</button></form>')
    table = _table(("Email", "Name", "Role", "Status", "Facilities", "Last login", ""), rows)
    return f'<section id="users" data-panel="users"><h2>Users</h2>{table}{invite}</section>'


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
    return f'<section id="pending" data-panel="pending"><h2>Pending drivers</h2>{table}</section>'


def _decided_section(conn: sqlite3.Connection) -> str:
    rows = []
    for d in onboarding.decided(conn):
        did = d["driver_id"]
        # Suspend an approved driver (the reject endpoint); re-approve a rejected one (the approve endpoint, licence checked).
        if d["approval_status"] == "APPROVED":
            action = _button("Suspend", f"/v1/drivers/{_segment(did)}/reject", confirm=f"Suspend {d['driver_name']}?")
        else:
            action = _button("Re-approve", f"/v1/drivers/{_segment(did)}/approve", confirm=f"Re-approve {d['driver_name']}?")
        rows.append([esc(did), esc(d["driver_name"]), esc(mask_phone(d["phone"])), esc(d["carrier_id"]), esc(d["vehicle_id"]),
                     esc(d["licence_expiry"]), esc(word(d["approval_status"])), action])
    table = _table(("Driver", "Name", "Phone", "Carrier", "Vehicle", "Licence expiry", "Approval", ""), rows,
                   "No onboarded drivers have been decided yet.")
    return f'<section id="decided" data-panel="decided"><h2>Onboarded drivers</h2>{table}</section>'


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
        rows.append([esc(when(r["ts"])), esc(r["actor_email"] or r["actor_user_id"] or ""), esc(r["action"]), esc(target),
                     esc(r["outcome"]), f'<code>{esc(detail)}</code>'])
    form = ('<form method="get" action="/admin#audit" class="filters">'
            f'<label>Actor <input name="actor" value="{esc(actor)}"></label>'
            f'<label>Action <input name="action" value="{esc(action)}"></label>'
            f'<label>Outcome <input name="outcome" value="{esc(outcome)}"></label>'
            f'<label>Since <input name="since" value="{esc(since)}" placeholder="2026-08-04T09:00:00+05:30"></label>'
            '<button type="submit">Filter</button></form>')
    table = _table(("Time", "Actor", "Action", "Target", "Outcome", "Detail"), rows, "No matching entries.")
    return f'<section id="audit" data-panel="audit"><h2>Audit log</h2>{form}{note}{table}</section>'


# ---- model tab (report only) ------------------------------------------------------------------------------
def _seconds(ms: float | None) -> str:
    return "-" if ms is None else f"{ms / 1000:.1f} s"


def _cost(t: CallTotals) -> str:
    # Never a made-up zero: without a price there is no cost to show.
    if t.cost_usd is None:
        return "price not set" if t.unpriced else "-"
    return f"${t.cost_usd:.4f}" + (" (some calls unpriced)" if t.unpriced else "")


def _model_in_use(gateway: ModelGateway, policy: ModelPolicy) -> str:
    provider, model_id = describe_model(gateway, policy)
    if provider == "stand-in":
        return model_id
    return f"{provider} / {model_id}" if model_id else "No model configured"


def _model_limits(p: ModelPolicy, env: Mapping[str, str] | None) -> str:

    def price(value: float) -> str:
        return f"USD {value:g}" if value > 0 else "price not set"

    items = (("Call timeout", f"{p.call_timeout_s:g} s"), ("Retries", str(p.retries)), ("Calls per message", str(p.max_rounds)),
             ("Tool runs per message", str(p.max_tool_executions)), ("Message deadline", f"{p.request_deadline_s:g} s"),
             ("Cost budget per message", f"USD {p.cost_budget_usd:g}"), ("Output length", f"{p.max_output_tokens} tokens"),
             ("Input price per million tokens", price(p.price_in_per_mtok)),
             ("Output price per million tokens", price(p.price_out_per_mtok)), ("LangSmith tracing", tracing_status(env).reason))
    return "<h3>Limits in force</h3>" + _table(("Limit", "Value"), [[esc(k), esc(v)] for k, v in items])


def _model_figures(conn: sqlite3.Connection, now: datetime) -> str:
    totals = (("Last 24 h", call_totals(conn, now.timestamp() - 86400)), ("All recorded", call_totals(conn, None)))
    calls = recent_calls(conn)
    health = "No model calls recorded yet."
    if calls:
        every = totals[1][1]
        health = (f"Last call {calls[0]['outcome']} at {when(calls[0]['ts'])}; "
                  f"{round(100 * every.failed / every.calls)}% of calls failed")
    rows = [[esc(label), esc(t.calls), esc(t.failed), esc(_seconds(t.p50_ms)), esc(_seconds(t.p95_ms)),
             esc(f"{t.input_tokens} / {t.output_tokens}"), esc(_cost(t))] for label, t in totals]
    summary = _table(("Period", "Calls", "Failed", "Typical", "95th", "Tokens in / out", "Est. cost"), rows)
    m = message_totals(conn)
    per_message = _table(
        ("Messages", "Typical", "95th", "Calls each", "Tokens in / out each", "Est. cost each"),
        [[esc(m.messages), esc(_seconds(m.p50_ms)), esc(_seconds(m.p95_ms)), esc(f"{m.avg_calls:.1f}"),
          esc(f"{m.avg_input_tokens:.0f} / {m.avg_output_tokens:.0f}"),
          esc("price not set" if m.avg_cost_usd is None else f"${m.avg_cost_usd:.4f}")]] if m.messages else [],
        "No model calls recorded yet.")
    turns = _table(
        ("Time", "Status", "Reason", "Handled in", "Calls", "Tokens in / out", "Est. cost"),
        [[esc(when(r["ts"])), esc(word(r["status"])), esc(why(r["reason_code"]) if r["reason_code"] else ""), esc(_seconds(r["duration_ms"])), esc(r["calls"]),
          esc(f"{r['input_tokens']} / {r['output_tokens']}"),
          esc("-" if r["cost_usd"] is None else f"${r['cost_usd']:.4f}")] for r in recent_turns(conn)],
        "No model calls recorded yet.")
    each = _table(
        ("Time", "Model", "Result", "Response time", "Tokens in / out", "Est. cost"),
        [[esc(when(r["ts"])), esc(f"{r['provider']} / {r['model_id']}"), esc(r["outcome"]), esc(_seconds(r["duration_ms"])),
          esc(f"{r['input_tokens']} / {r['output_tokens']}"),
          esc("-" if r["cost_usd"] is None else f"${r['cost_usd']:.4f}")] for r in calls],
        "No model calls recorded yet.")
    return (f'<p>Health: {esc(health)}</p>{summary}'
            f'<h3>Per driver message</h3>{per_message}'
            f'<h3>Recent messages</h3>{turns}<h3>Recent calls</h3>{each}')


_LANGSMITH_PANEL = (
    '<h3>LangSmith</h3><p class="note">LangSmith traces the conversation loop, not the provider request: it has no reliable token or '
    'cost figures, and a handled model failure can appear as a successful run.</p>'
    '<button type="button" id="langsmith-load">Load from LangSmith</button><div id="langsmith-result"></div>')


def _model_section(conn: sqlite3.Connection, deps: AppDeps) -> str:
    try:
        figures = _model_figures(conn, deps.clock.now())
    # A database from before migration 014: say so here, and leave the rest of the screen working.
    except sqlite3.OperationalError as error:
        if "no such table" not in str(error):
            raise
        figures = '<p class="empty">This database has no model records yet. Apply the migrations, then restart.</p>'
    gateway, policy = model_in_force(deps, conn)
    return ('<section id="model" data-panel="model"><h2>Model</h2>'
            f"<p>In use: {esc(_model_in_use(gateway, policy))}</p>{figures}{_model_limits(policy, env_in_force(deps, conn))}{_LANGSMITH_PANEL}</section>")


_FROM = {"saved": "saved here", "env": ".env", "unset": "not set"}


def _configuration_section(conn: sqlite3.Connection, deps: AppDeps) -> str:
    """Model, LangSmith and Telegram settings. Secrets are never drawn: only that one is set, and its last characters."""
    view = settings_view(deps, conn)
    settings: dict[str, dict[str, Any]] = view["settings"]
    live = bool(view["live"])
    keys_off = view["master_key"] != "ok"

    def source(name: str) -> str:
        return f'<small class="note">{esc(_FROM[settings[name]["source"]])}</small>'

    def value(name: str) -> str:
        return str(settings[name].get("value", ""))

    def text(label: str, field: str, name: str) -> str:
        return f'<label>{esc(label)} <input name="{field}" value="{esc(value(name))}" autocomplete="off">{source(name)}</label>'

    def choice(label: str, field: str, name: str, options: list[tuple[str, str]], selected: str) -> str:
        return f'<label>{esc(label)} <select name="{field}">{_options(options, selected)}</select>{source(name)}</label>'

    def secret(label: str, field: str, name: str) -> str:
        entry = settings[name]
        state = "not set"
        if entry["set"]:
            ending = f", ends in {entry['last4']}" if entry.get("last4") else ""
            state = f"set{ending} ({_FROM[entry['source']]})"
        disabled = " disabled" if keys_off else ""
        return (f'<label>{esc(label)} <input name="{field}" type="password" autocomplete="new-password"{disabled}>'
                f'<small class="note">{esc(state)}. Leave empty to keep.</small></label>')

    def block(section: str, title: str, fields: str, buttons: str, note: str = "") -> str:
        extra = f'<p class="note">{esc(note)}</p>' if note else ""
        if not live:
            return f"<h3>{esc(title)}</h3>{extra}<form>{fields}</form>"
        reset = _button("Reset to .env", f"/v1/admin/settings/{section}/reset", body={},
                        confirm=f"Remove the saved {title} settings and use .env?")
        return (f'<h3>{esc(title)}</h3>{extra}<div><form data-settings="{section}">{fields}{buttons}'
                f'<button type="submit">Save</button>{reset}</form><p class="note" data-result></p></div>')

    def check(label: str, path: str) -> str:
        return f'<button type="button" data-check="/v1/admin/settings/{path}">{esc(label)}</button>'

    notes = []
    if not live:
        notes.append("Changes are for the live service. This screen shows the environment only.")
    if view["master_key"] == "missing":
        notes.append("Keys cannot be saved: SETUHAUL_SETTINGS_KEY is not set in .env.")
    elif view["master_key"] == "invalid":
        notes.append("Keys cannot be saved: SETUHAUL_SETTINGS_KEY in .env is not a valid key.")
    if view["unreadable"]:
        notes.append("A saved key cannot be read. Enter it again or reset to .env.")
    provider = value("SETUHAUL_MODEL_PROVIDER")
    providers = ([] if provider in ("anthropic", "openai") else [(provider, provider or "not set")])
    model = block("model", "Model", (
        choice("Provider", "provider", "SETUHAUL_MODEL_PROVIDER",
               providers + [("openai", "openai"), ("anthropic", "anthropic")], provider)
        + text("Model ID", "model_id", "SETUHAUL_MODEL_ID")
        + secret("API key", "api_key", "SETUHAUL_MODEL_API_KEY")
        + text("Reasoning effort", "reasoning_effort", "SETUHAUL_OPENAI_REASONING_EFFORT")
        + text("Input price per million tokens", "price_in", "SETUHAUL_MODEL_PRICE_IN_PER_MTOK")
        + text("Output price per million tokens", "price_out", "SETUHAUL_MODEL_PRICE_OUT_PER_MTOK")),
        check("Test connection", "model/test"), "A new provider needs its own API key.")
    on = value("SETUHAUL_LANGSMITH_ENABLED").strip().lower() in ("true", "1", "yes", "on")
    # Show what is happening; only while nothing is set and nothing is sent is hiding drawn as the starting choice.
    untouched = settings["SETUHAUL_LANGSMITH_HIDE_TEXT"]["source"] == "unset" and not tracing_status(env_in_force(deps, conn)).enabled
    hide = "on" if untouched or value("SETUHAUL_LANGSMITH_HIDE_TEXT") == "1" else "off"
    tracing = block("langsmith", "LangSmith", (
        choice("Send traces", "enabled", "SETUHAUL_LANGSMITH_ENABLED", [("off", "Off"), ("on", "On")], "on" if on else "off")
        + choice("Hide message text", "hide_text", "SETUHAUL_LANGSMITH_HIDE_TEXT", [("on", "On"), ("off", "Off")], hide)
        + secret("API key", "api_key", "LANGSMITH_API_KEY")
        + text("Project", "project", "LANGSMITH_PROJECT")), "",
        f"Tracing is {tracing_status(env_in_force(deps, conn)).reason}.")
    webhook = {"registered": "The webhook is registered.", "needs_registering": "The webhook needs registering.",
               "no_public_url": "The webhook cannot be registered: SETUHAUL_PUBLIC_URL is not set."}[view["webhook"]]
    bot = block("telegram", "Telegram", (
        secret("Bot token", "bot_token", "SETUHAUL_TELEGRAM_BOT_TOKEN")
        + secret("Webhook secret", "webhook_secret", "SETUHAUL_TELEGRAM_WEBHOOK_SECRET")),
        ("" if keys_off else '<button type="button" data-generate>Save with a new secret</button>')
        + check("Test bot", "telegram/test") + check("Register webhook", "telegram/register-webhook"),
        f"{webhook} Each driver has to open a new bot and press Start before it can message them.")
    top = "".join(f'<p class="note">{esc(n)}</p>' for n in notes)
    return f'<section id="config" data-panel="config"><h2>Configuration</h2>{top}{model}{tracing}{bot}</section>'


def _cards(items: list[tuple[str, str, int]]) -> str:
    cards = "".join(f'<li><a href="{esc(target)}" class="{"hot" if n else "calm"}"><b>{n}</b><span>{label}</span></a></li>'
                    for target, label, n in items)
    return f'<ul class="attention" aria-label="Needs attention">{cards}</ul>'


def _admin_attention(conn: sqlite3.Connection, me: User, pending: int) -> str:
    # What is waiting anywhere. A case that names no load has no facility, so only an admin can ever see or work it.
    unowned = conn.execute("select count(*) from escalation_cases where status in ('OPEN','CLAIMED') and shipment_id is null").fetchone()[0]
    return _cards([
        ("#pending", "Drivers awaiting approval", pending),
        ("/warehouse#messages", "Open cases", len(case_ids_in_scope(conn, me, ("OPEN", "CLAIMED")))),
        ("/warehouse#messages", "Cases with no load", unowned),
        ("/warehouse#messages", "Facility requests", len(pending_changes_in_scope(conn, me))),
    ])


def render_admin(conn: sqlite3.Connection, me: User, query: dict[str, str], deps: AppDeps) -> str:
    # The admin screen: users, pending drivers, decided onboarded drivers (suspend / re-approve), audit log.
    nav = '<a href="/admin">Admin</a> <a href="/warehouse">Warehouse</a> <a href="/wiki">Wiki</a>'
    waiting = len(onboarding.pending(conn))
    tabs = _tabs((("users", "Users"), ("pending", "Pending drivers"), ("decided", "Onboarded drivers"), ("deleted", "Deleted drivers"), ("audit", "Audit log"), ("model", "Model"),
                  ("config", "Configuration")), {"pending": waiting})
    # The "wh" class gives the same tab styling as the warehouse screen.
    # Imported here: that module uses this one's page helpers.
    from setuhaul.auth.exception_pages import deleted_section

    body = ('<div class="wh">' + _admin_attention(conn, me, waiting) + tabs + _users_section(conn, me) + _pending_section(conn) + _decided_section(conn)
            + deleted_section(conn)
            + _audit_section(conn, query) + _model_section(conn, deps)
            + _configuration_section(conn, deps) + "</div>")
    return _layout("Admin", body, ("common.js", "tabs.js", "admin.js", "model.js", "settings.js"), me, nav)


# ---- warehouse screen -------------------------------------------------------------------------------------
_STATUS_WORDS = {"AVAILABLE": "Free", "BOOKED": "Booked", "HELD": "Held", "BLOCKED": "Blocked"}


def _who(label: str) -> str:
    # A booking label is "driver · order": the name on one line, the order under it.
    name, _, order = label.partition(" \u00b7 ")
    return f"<strong>{esc(name)}</strong>" + (f"<span>{esc(order)}</span>" if order else "")


def _slot_button(dock: str, slot: dict[str, Any]) -> str:
    # One compact cell: initials to look at, the whole story for hover, screen readers and the detail box.
    name, _, order = slot["label"].partition(" \u00b7 ")
    occupied = bool(order)
    state = _STATUS_WORDS.get(slot["status"], "Blocked")
    parts = [state] + ([name, order] if occupied else [])
    if slot["status"] == "BLOCKED":
        reason = slot.get("reason") or ("" if occupied else slot["label"])
        parts += [reason] if reason else []
    detail = f"{dock} {slot['start']}-{slot['end']}: {', '.join(parts)}"
    initials = "".join(word[0] for word in name.split()[:2]).upper() if occupied else ""
    free = ""
    if slot.get("freeable"):
        ask = f"Free {dock} {slot['start']} for {order} ({name})? The shipment stays and will need a new slot."
        free = (f' data-free="/v1/warehouse/appointments/{_segment(slot["appointment_id"])}/free"'
                f' data-confirm="{esc(ask)}"')
    return (f'<button type="button" class="slot" data-key="{esc("slot:" + str(slot.get("slot_id", detail)))}"'
            f' data-detail="{esc(detail)}" title="{esc(detail)}" aria-label="{esc(detail)}"{free}>{esc(initials)}</button>')


# Up to this many facilities are offered as buttons; more would not fit on one line.
_FACILITY_BUTTONS = 5


def _facility_chooser(options: list[tuple[str, str]], selected: str | None) -> str:
    shown = f' data-facility="{esc(selected or "")}"'
    if len(options) > _FACILITY_BUTTONS:
        return (f'<form method="get" action="/warehouse" class="filters facilities"{shown}><label>Facility '
                f'<select name="facility">{_options(options, selected)}</select></label><button type="submit">Show</button></form>')
    links = "".join(
        f'<a href="/warehouse?facility={_segment(value)}#board"{" aria-current=\"true\"" if value == selected else ""}>'
        f'{esc(label.partition(" - ")[2] or label)}</a>' for value, label in options)
    return f'<nav class="facilities" aria-label="Facility"{shown}>{links}</nav>'


# The detail of one cell. It sits outside everything the live refresh replaces.
_SLOT_DETAIL = ('<dialog id="slot-detail" aria-labelledby="slot-detail-text"><p id="slot-detail-text"></p>'
                '<button type="button" data-slot-free hidden>Free this slot</button>'
                '<form method="dialog"><button type="submit">Close</button></form></dialog>')


def _bay_card(dock: dict[str, Any], moment: datetime) -> str:
    # What a worker on the floor asks: what is on this bay now, and what comes next. Judged on real start and end times,
    # not the clock hour, because a booking can start at half past.
    spans = [(datetime.fromisoformat(s["start_ts"]), datetime.fromisoformat(s["end_ts"]), s) for s in dock["slots"]]
    taken = ("BOOKED", "HELD")
    # If slots overlap, an occupied one wins over a blocked one, and a blocked one over a free one.
    covering = sorted((s for start, end, s in spans if start <= moment < end),
                      key=lambda s: 0 if s["status"] in taken else 1 if s["status"] == "BLOCKED" else 2)
    now = covering[0] if covering else None
    today = moment.astimezone(IST).date()
    ahead = sorted(((start, s) for start, _, s in spans
                    if start > moment and start.astimezone(IST).date() == today and s["status"] in taken), key=lambda p: p[0])
    later = ahead[0][1] if ahead else None
    kind = _CELL_CLASSES.get(now["status"], "blocked") if now else "none"
    state = _STATUS_WORDS.get(now["status"], "Blocked") if now else "No slot"
    occupant = _who(now["label"]) if now and now["status"] in ("BOOKED", "HELD") else ""
    upcoming = f"Next {esc(later['start'])} {esc(later['label'].partition(' \u00b7 ')[0])}" if later else "Nothing else booked"
    return (f'<li class="bay cell-{kind}"><div class="bay-head"><b>{esc(dock["code"])}</b><span class="pill">{esc(state)}</span></div>'
            f'<div class="occupant">{occupant}</div><div class="next">{upcoming}</div></li>')


def _board_section(board: dict[str, Any] | None, options: list[tuple[str, str]], selected: str | None, moment: datetime,
                   blocks: str = "", ops: str = "") -> str:
    selector = _facility_chooser(options, selected)
    if board is None:
        return (f'<section id="board" data-panel="board"><h2>Dock board</h2>{selector}'
                f'<div class="board-layout"><div class="board-main"><p class="empty">No facility to show.</p></div>{ops}</div></section>')
    hours, current = board["hours"], moment.astimezone(IST).hour
    head = '<th scope="col">Dock</th>' + "".join(
        f'<th scope="col"{" class=\"now\"" if h == current else ""}>{esc(f"{h:02d}:00")}</th>' for h in hours)
    rows = []
    for dock in board["docks"]:
        cells = []
        for hour in hours:
            slots = [s for s in dock["slots"] if s["hour"] == hour]
            # One cell per hour; its class comes from a fixed whitelist, never from data.
            kind = _CELL_CLASSES.get(slots[0]["status"], "blocked") if slots else "none"
            past = " past" if slots and all(s["past"] for s in slots) else ""
            marker = " now" if hour == current else ""
            text = "".join(_slot_button(dock["code"], s) for s in slots)
            cells.append(f'<td class="cell cell-{kind}{past}{marker}">{text}</td>')
        rows.append(f'<tr><th scope="row"><b>{esc(dock["code"])}</b><span>{esc(dock["type"])}</span></th>{"".join(cells)}</tr>')
    table = f'<div class="scroll"><table class="board"><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    legend = "".join(f'<span class="legend-key cell-{c}">{w}</span>' for c, w in
                     (("available", "Free"), ("booked", "Booked"), ("held", "Held, not yet confirmed"), ("blocked", "Blocked")))
    bays = "".join(_bay_card(d, moment) for d in board["docks"])
    below = (f'<details data-key="bays"><summary>On the bays now</summary><ul class="bays">{bays}</ul></details>'
             + (f'<details data-key="blocking"><summary>Block a dock</summary>{blocks}</details>' if blocks else ""))
    return (f'<section id="board" data-panel="board"><h2>Dock board</h2>{selector}'
            f'<p class="legend asof">Facility {esc(board["facility"])}, {esc(f"{moment.astimezone(IST):%a %d %b, %H:%M}")}</p>'
            f'<div class="board-layout"><div class="board-main"><p class="keys">{legend}</p>'
            f'<p id="block-result" class="note" role="status"></p>{table}{_SLOT_DETAIL}{below}</div>{ops}</div></section>')


def _waiting_for_partner(board: dict[str, Any] | None) -> str:
    # Requests for this facility still waiting for a partner's reply: never shown as booked slots.
    if board is None:
        return ""
    rows = [[esc(p["request_ref"]), esc(p["order_reference"]), esc(p["driver"]), esc(f"{p['start']}-{p['end']}")]
            for p in board["pending_partner"]]
    table = _table(("Request", "Order", "Driver", "Proposed"), rows, "No requests are waiting for a partner.")
    return f'<h3>Waiting for partner reply</h3><div class="waiting">{table}</div>'


def _clock(stamp: str) -> str:
    # "04 Aug 09:35" for people; an unreadable value is shown as it is.
    try:
        return f"{datetime.fromisoformat(stamp).astimezone(IST):%d %b %H:%M}"
    except ValueError:
        return stamp


def _short(text: str, limit: int = 90) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "\u2026"


# The outcomes a coordinator may record, in plain words (the codes are the service's RESOLUTION_CODES).
_OUTCOMES = (("ASSIGNED_SLOT", "Assigned a slot"), ("ADVISED_DRIVER", "Advised the driver"),
             ("CANCELLED_BY_OPERATIONS", "Cancelled by operations"), ("NO_ACTION_NEEDED", "No action needed"), ("OTHER", "Other"))


def _named(name: str, ident: str) -> str:
    # The name a person knows, with the id beside it for reference.
    return f'{esc(name)} <span class="id">{esc(ident)}</span>'


def _case_actions(c: CaseView, me: str, owner: str) -> str:
    # Only what the service would accept from this user is drawn: claim an open case, resolve your own claim, reopen a resolved one.
    cid = c.case_id
    base = f"/v1/coordinator/cases/{_segment(cid)}"
    if c.status == "OPEN":
        return _button("Claim", f"{base}/claim", done="Case claimed.", key=f"claim:{cid}")
    if c.status == "RESOLVED":
        return _button("Reopen", f"{base}/reopen", confirm=f"Reopen case {cid}?", done="Case reopened.", key=f"reopen:{cid}")
    if c.claimed_by != me:
        return f'<p class="note">{esc(owner)} is working this case.</p>'
    # Nothing is preselected or prefilled: the driver receives exactly what the coordinator writes.
    options = '<option value="" selected disabled>Choose an outcome</option>' + _options(list(_OUTCOMES))
    return (f'<form class="case-resolve" data-api="{esc(base)}/resolve" '
            f'data-confirm="{esc(f"Send this message to the driver and close case {cid}?")}" '
            'data-done="Case resolved. Your message was added to the driver\'s chat.">'
            f'<label>Outcome <select name="resolution_code" required data-key="{esc(f"outcome:{cid}")}">{options}</select></label>'
            f'<label>Message to the driver <textarea name="note" rows="3" maxlength="500" required data-key="{esc(f"note:{cid}")}">'
            f'</textarea></label><button type="submit" data-key="{esc(f"resolve:{cid}")}">Resolve</button></form>')


def _case_history(conn: sqlite3.Connection, case_id: str) -> str:
    # Who did what and when, folded away. A stored web identity is shown as the person's name.
    lines = []
    for e in case_events(conn, case_id):
        who = claim_label(conn, e["actor_id"]) if e["actor_type"] == "COORDINATOR" else str(e["actor_type"]).lower()
        what = str(e["event_type"]).replace("_", " ").capitalize()
        note = f": {e['note']}" if e["note"] else ""
        lines.append(f"<li><time>{esc(_clock(e['occurred_at']))}</time> {esc(what)} by {esc(who)}{esc(note)}</li>")
    if not lines:
        return ""
    return (f'<details class="history" data-key="{esc(f"history:{case_id}")}"><summary data-key="{esc(f"history-toggle:{case_id}")}">History</summary>'
            f'<ol>{"".join(lines)}</ol></details>')


def _case_cards(conn: sqlite3.Connection, user: User, cases: list[CaseView], empty: str) -> str:
    if not cases:
        return f'<p class="empty">{esc(empty)}</p>'
    me = WEB_PREFIX + user.email
    cards = []
    for c in cases:
        # A long note is folded to one line; opening it shows the whole text.
        summary = c.summary or ""
        note = (f'<details class="note-fold" data-key="{esc(f"summary:{c.case_id}")}"><summary data-key="{esc(f"summary-toggle:{c.case_id}")}">{esc(_short(summary))}</summary>'
                f"<p>{esc(summary)}</p></details>" if len(summary) > 90 else f"<p>{esc(summary)}</p>")
        owner = claim_label(conn, c.claimed_by) or ""
        claimed = f"Claimed by {owner}" if owner else "Not claimed"
        outcome = (f'<p>{esc(dict(_OUTCOMES).get(c.resolution_code or "", c.resolution_code))}: {esc(c.resolution_note)}</p>'
                   if c.status == "RESOLVED" else "")
        # The closing comment marks where one card ends, for the tests.
        cards.append(f'<li class="case" data-key="{esc(f"case:{c.case_id}")}" tabindex="-1"><div class="case-head"><b>{esc(c.case_id)}</b>'
                     f'<span class="pill">{esc(word(c.status))}</span><span class="reason" title="{esc(c.reason_code)}">{esc(why(c.reason_code))}</span></div>{note}{outcome}'
                     f'<div class="meta"><span>Load {esc(c.shipment_id or "none")}</span><span>{esc(claimed)}</span></div>'
                     f"{_case_history(conn, c.case_id)}{_case_actions(c, me, owner)}</li><!--case-->")
    return f'<ul class="cases">{"".join(cards)}</ul>'


def _recently_resolved(conn: sqlite3.Connection, user: User, now: datetime) -> str:
    # Resolved cases that may still be reopened, newest first, folded away.
    since = now - timedelta(hours=load_settings().escalation_reopen_window_hours)
    ids = case_ids_in_scope(conn, user, ("RESOLVED",), resolved_since=since.isoformat())
    recent = [c for c in (get_case(conn, i) for i in ids) if c is not None]
    if not recent:
        return ""
    recent.sort(key=lambda c: c.resolved_at or since, reverse=True)
    return (f'<details class="resolved" data-key="resolved"><summary data-key="resolved-toggle">Recently resolved ({len(recent)})</summary>'
            f'{_case_cards(conn, user, recent, "")}</details>')


def _chat_browser(chats: list[dict[str, Any]]) -> str:
    # A list of drivers beside one conversation at a time, each in its own scrolling box, so long chats never stretch the page.
    if not chats:
        return '<p class="empty">No chats to show.</p>'
    names, threads = [], []
    roles = {"DRIVER": ("driver", "Driver"), "AGENT": ("agent", "Assistant")}
    for number, d in enumerate(chats):
        name = d["driver_name"] or d["driver_id"]
        last = d["messages"][-1] if d["messages"] else {"text": "", "timestamp": ""}
        names.append(f'<li><button type="button" data-thread="{number}" data-driver="{esc(d["driver_id"])}"><span class="top"><b>{esc(name)}</b>'
                     f'<time>{esc(_clock(last["timestamp"]))}</time></span><span class="preview">{esc(_short(last["text"]))}</span></button></li>')
        bubbles = []
        for m in d["messages"]:
            css, who = roles.get(m["role"], ("operations", "Operations"))
            bubbles.append(f'<li class="bubble {css}"><span class="role">{who}</span><p>{esc(m["text"])}</p>'
                           f'<time>{esc(_clock(m["timestamp"]))}</time></li>')
        threads.append(f'<div class="thread" id="thread-{number}"><h4>{esc(name)} <span>{esc(d["driver_id"])}</span></h4>'
                       f'<ol>{"".join(bubbles)}</ol></div>')
    return (f'<div class="chats"><ul class="chat-list">{"".join(names)}</ul><div class="threads">{"".join(threads)}</div></div>')


def _facility_requests(conn: sqlite3.Connection, user: User) -> str:
    # Requests to move a load HERE: approving moves it and offers the driver a slot.
    rows = []
    for r in pending_changes_in_scope(conn, user):
        base = f"/v1/warehouse/facility-changes/{_segment(r['request_id'])}"
        actions = (_button("Approve", f"{base}/approve", confirm=f"Move {r['order_reference']} to {r['destination']}?", key=f"approve:{r['request_id']}")
                   + _button("Reject", f"{base}/reject", confirm=f"Refuse the move of {r['order_reference']}?", key=f"reject:{r['request_id']}"))
        rows.append([esc(r["request_id"]), esc(r["driver_name"]), esc(r["order_reference"]), esc(r["origin"]), esc(r["destination"]), actions])
    table = _table(("Request", "Driver", "Order", "From", "To", ""), rows, "No requests are waiting.")
    return f'<h3>Facility change requests</h3>{table}'


def _messages_section(conn: sqlite3.Connection, user: User, now: datetime, waiting_for_partner: str = "") -> str:
    """The Operations panel beside the board: cases, driver chats and requests."""
    found = [c for c in (get_case(conn, i) for i in case_ids_in_scope(conn, user, ("OPEN", "CLAIMED"))) if c is not None]
    partners = partner_requests_in_scope(conn, user)
    reqs = [[esc(r["request_ref"]), _named(r["facility_name"], r["facility_id"]), esc(r["shipment_id"]), esc(word(r["status"])),
             esc(word(r["email_status"])), esc(f"{when(r['proposed_start'])} to {_clock(r['proposed_end'])}")] for r in partners]
    note = ('<p class="note">Drivers on projects created by an administrator do not appear in a warehouse authority\'s chats; '
            "only drivers on projects you created are shown.</p>")
    chats = driver_chats_in_scope(conn, user, now)
    waiting = len(pending_changes_in_scope(conn, user)) + sum(1 for r in partners if r["status"] == "PENDING_PARTNER")
    views = (
        ("cases", "Cases", len(found), '<h3>Open cases</h3>' + _case_cards(conn, user, found, "No open cases.")
         + _recently_resolved(conn, user, now)),
        ("chats", "Chats", len(chats), f"<h3>Driver chats</h3>{note}{_chat_browser(chats)}"),
        ("requests", "Requests", waiting, _facility_requests(conn, user) + "<h3>Partner slot requests</h3>"
         + _table(("Request", "Facility", "Load", "Status", "Email", "Proposed"), reqs, "No partner requests.")
         + waiting_for_partner),
    )
    buttons = "".join(f'<button type="button" data-view="{key}" data-key="view:{key}">{label} <span class="count">{n or ""}</span></button>'
                      for key, label, n, _ in views)
    panes = "".join(f'<div data-pane="{key}">{body}</div>' for key, _, _, body in views)
    return (f'<aside id="messages" class="ops" aria-label="Operations" data-count="{len(found)}"><h2>Operations</h2>'
            f'<div class="views">{buttons}</div>{panes}</aside>')


def _attention(conn: sqlite3.Connection, user: User, today: date) -> str:
    # The supervisor's first question: is anything waiting for me?
    drivers = driver_list(conn, user, today)
    items = [
        ("messages", "Open cases", len(case_ids_in_scope(conn, user, ("OPEN", "CLAIMED")))),
        ("messages", "Waiting for partner",
         sum(1 for r in partner_requests_in_scope(conn, user, MAX_LIMIT) if r["status"] == "PENDING_PARTNER")),
        ("drivers", "Drivers awaiting approval", sum(1 for d in drivers if d["approval_status"] == "PENDING_APPROVAL")),
        ("drivers", "Licences not valid", sum(1 for d in drivers if not d["licence_valid"])),
        ("messages", "Facility requests", len(pending_changes_in_scope(conn, user))),
    ]
    return _cards([(f"#{target}", label, n) for target, label, n in items])


def _projects_section(conn: sqlite3.Connection, user: User, today: date, facility_options: list[tuple[str, str]],
                      chosen: str | None = None) -> str:
    # Drivers who may be allocated: in the user's scope and eligible to work today.
    eligible = [(r["driver_id"], f"{r['driver_id']} - {r['driver_name']}") for r in drivers_in_scope(conn, user, today)
                if onboarding.driver_eligible(conn, r["driver_id"], today)]
    rows = []
    names = {r["driver_id"]: r["driver_name"] for r in conn.execute("select driver_id, driver_name from drivers")}
    places = {r["facility_id"]: r["facility_name"] for r in conn.execute("select facility_id, facility_name from facilities")}
    for p in projects.list_projects(conn, user):
        pid = str(p["project_id"])
        active = [str(d) for d in p["driver_ids"]]  # type: ignore[attr-defined]
        drivers = "".join(f"<div>{_named(names.get(d, d), d)} " + _button("Release", f"/v1/projects/{_segment(pid)}/release", {"driver_id": d},
                          f"Release {names.get(d, d)} from {p['customer_name']}?") + "</div>" for d in active)
        allocate = ""
        free = [(v, label) for v, label in eligible if v not in active]
        if p["status"] == "ACTIVE" and free:
            allocate = (f'<form data-api="/v1/projects/{esc(_segment(pid))}/allocate"><select name="driver_id" aria-label="Driver to allocate">{_options(free)}</select>'
                        '<button type="submit">Allocate</button></form>')
        rows.append([esc(pid), esc(p["customer_name"]), esc(p["contract_ref"]),
                     _named(places.get(str(p["facility_id"]), str(p["facility_id"])), str(p["facility_id"])), esc(word(p["status"])),
                     esc(f"{p['starts_on']} - {p['ends_on'] or 'open'}"), drivers + allocate])
    create = ('<h3>New project</h3><form data-api="/v1/projects"><label>Customer <input name="customer_name" required maxlength="120"></label>'
              '<label>Contract reference <input name="contract_ref" required maxlength="64"></label>'
              f'<label>Facility <select name="facility_id">{_options(facility_options, chosen)}</select></label>'
              f'<label>Starts on <input name="starts_on" type="date" value="{esc(today.isoformat())}" required></label>'
              '<label>Ends on <input name="ends_on" type="date"></label><button type="submit">Create</button></form>')
    table = _table(("Project", "Customer", "Contract", "Facility", "Status", "Dates", "Drivers"), rows, "No projects yet.")
    return f'<section id="projects" data-panel="projects"><h2>Projects</h2><p class="note">This tab does not update by itself. Reload the page to see changes.</p>{table}{create}</section>'


def _drivers_section(conn: sqlite3.Connection, user: User, today: date) -> str:
    rows = []
    for d in driver_list(conn, user, today):
        base = f"/v1/warehouse/drivers/{_segment(d['driver_id'])}/telegram"
        action = ""
        # Revoke and allow re-link only where the server would allow them (admin, or the authority who onboarded the driver).
        if telegram.may_manage_binding(conn, user, d["driver_id"]):
            if d["telegram_status"] == "BOUND":
                action = _button("Revoke Telegram", f"{base}/revoke", confirm=f"Revoke Telegram for {d['driver_name']}?")
            elif d["telegram_status"] == "REVOKED":
                action = _button("Allow re-link", f"{base}/allow-relink",
                                 confirm=f"Let {d['driver_name']} link a Telegram account again?")
        # Deleting follows the same rule: an admin, or the authority who added the driver.
        if telegram.may_manage_binding(conn, user, d["driver_id"]):
            action += f'<button type="button" data-delete-driver="{esc(d["driver_id"])}">Delete</button>'
        rows.append([esc(d["driver_id"]), esc(d["driver_name"]), esc(d["phone"]), esc(word(d["approval_status"])),
                     "Yes" if d["licence_valid"] else "No", esc(word(d["telegram_status"])), action])
    carriers = [(r["carrier_id"], f"{r['carrier_id']} - {r['carrier_name']}")
                for r in conn.execute("select carrier_id, carrier_name from carriers where active_flag=1 order by carrier_id")]
    vehicles = conn.execute("select vehicle_id, carrier_id, registration_number from vehicles where active_flag=1 "
                            "order by carrier_id, vehicle_id").fetchall()
    vehicle_options = _options([(v["vehicle_id"], f"{v['vehicle_id']} - {v['registration_number']} ({v['carrier_id']})") for v in vehicles],
                               extra={v["vehicle_id"]: v["carrier_id"] for v in vehicles})
    add = ('<h3>Add a driver</h3><p class="note">New drivers wait for an administrator to approve them.</p>'
           '<form data-api="/v1/drivers" data-limit><label>Name <input name="name" required maxlength="100"></label>'
           '<label>Phone <input name="phone" required maxlength="32" autocomplete="off"></label>'
           '<label>Licence number <input name="licence_number" required maxlength="40" autocomplete="off"></label>'
           '<label>Licence expiry <input name="licence_expiry" type="date" required></label>'
           f'<label>Carrier <select name="carrier_id" id="carrier">{_options(carriers)}</select></label>'
           f'<label>Vehicle <select name="vehicle_id" id="vehicle">{vehicle_options}</select></label>'
           '<button type="submit">Add driver</button></form>')
    table = _table(("Driver", "Name", "Phone", "Approval", "Licence valid", "Telegram", ""), rows, "No drivers yet.")
    return f'<section id="drivers" data-panel="drivers"><h2>Drivers</h2><p class="note">This tab does not update by itself. Reload the page to see changes.</p>{table}<div id="driver-removal" role="region" aria-live="polite"></div>{add}</section>'


def render_warehouse(conn: sqlite3.Connection, user: User, now: Any, facility: str | None,
                     remembered: str | None = None) -> HTMLResponse:
    # The facilities the user may pick from: an admin all of them, anyone else only their own.
    if user.role == "ADMIN":
        wanted = conn.execute("select facility_id, facility_name from facilities order by facility_id").fetchall()
    else:
        marks = ",".join("?" * len(user.facilities))
        wanted = conn.execute(f"select facility_id, facility_name from facilities where facility_id in ({marks}) order by facility_id",
                              user.facilities).fetchall()
    options = [(r["facility_id"], f"{r['facility_id']} - {r['facility_name']}") for r in wanted]
    # The facility last looked at, but only while it is still one of the user's.
    if facility is None and remembered in {value for value, _ in options}:
        facility = remembered
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
    # Imported here: the tab's module uses this one's page helpers.
    from setuhaul.auth.exception_pages import blocks_section
    from setuhaul.auth.shipment_pages import shipments_section

    blocks = "" if chosen is None else blocks_section(conn, user, chosen, now)

    tabs = _tabs((("board", "Dock board"), ("projects", "Projects"), ("shipments", "Shipments"),
                  ("drivers", "Drivers")),
                 {"board": len(case_ids_in_scope(conn, user, ("OPEN", "CLAIMED"))) + len(pending_changes_in_scope(conn, user))})
    # warehouse.js writes how fresh the live parts are here.
    fresh = '<p class="live" data-live><span data-live-state role="status"></span> <span data-live-time></span></p>'
    body = ('<div class="wh">' + _attention(conn, user, today) + tabs + fresh
            + _board_section(board, options, chosen, now, blocks, _messages_section(conn, user, now, _waiting_for_partner(board)))
            + _projects_section(conn, user, today, options, chosen) + shipments_section(conn, user, now, options)
            + _drivers_section(conn, user, today) + "</div>")
    nav = ('<a href="/warehouse">Warehouse</a>' + (' <a href="/admin">Admin</a>' if user.role == "ADMIN" else "")
           + ' <a href="/wiki">Wiki</a>')
    return _respond(_layout("Warehouse", body, ("common.js", "tabs.js", "warehouse.js", "exceptions.js"), user, nav))


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
                return _redirect({"ADMIN": "/admin", "SECURITY": "/gate"}.get(user.role, "/warehouse"))
        # The same condition the Google routes use (a client and a signing secret of at least 32 bytes).
        google = deps.google is not None and len(deps.session_secret) >= 32
        # Only a listed category selects a message; the parameter itself is never shown.
        notice = NOTICES.get(request.query_params.get("signin", ""), "")
        return _respond(_landing(google, deps.local_admin_enabled, notice))

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(request: Request) -> Response:
        user = guarded(request, "ADMIN")
        if isinstance(user, Response):
            return user
        query = {k: v for k, v in request.query_params.items()}
        return read_page(user, lambda conn: _respond(render_admin(conn, user, query, deps)))

    @app.get("/warehouse", response_class=HTMLResponse)
    def warehouse_page(request: Request) -> Response:
        user = guarded(request, "WAREHOUSE_AUTHORITY", "ADMIN")
        if isinstance(user, Response):
            return user
        facility = request.query_params.get("facility") or None
        remembered = request.cookies.get(FACILITY_COOKIE)
        page = read_page(user, lambda conn: render_warehouse(conn, user, deps.clock.now(), facility, remembered))
        # Remember a choice the user made and could see; the live refresh asks for the same page and must not move it.
        if facility and page.status_code == 200 and "x-live" not in request.headers:
            page.set_cookie(FACILITY_COOKIE, facility, max_age=30 * 86400, path="/warehouse", httponly=True, samesite="lax")
        return page

    @app.get("/gate", response_class=HTMLResponse)
    def gate_page(request: Request) -> Response:
        user = guarded(request, "SECURITY", "ADMIN")
        if isinstance(user, Response):
            return user
        # Imported here: the screen's module uses this one's page helpers.
        from setuhaul.auth.gate_pages import render_gate

        return read_page(user, lambda conn: render_gate(conn, user, deps.clock))

    @app.get("/wiki", response_class=HTMLResponse)
    def wiki_page(request: Request) -> Response:
        user = guarded(request, "WAREHOUSE_AUTHORITY", "SECURITY", "ADMIN")
        if isinstance(user, Response):
            return user
        # Imported here: the page's module uses this one's helpers. No database is needed: the text is in the code.
        from setuhaul.auth.wiki_pages import render_wiki

        return render_wiki(user)

    @app.get("/app/assets/{name}")
    def asset(name: str) -> Response:
        # Only the four known files, by exact name: nothing the caller types ever becomes a path.
        kind = ASSET_TYPES.get(name)
        if kind is None:
            return Response("Not found", status_code=404, media_type="text/plain",
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})
        return Response((ASSET_DIR / name).read_bytes(), media_type=kind,
                        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})
