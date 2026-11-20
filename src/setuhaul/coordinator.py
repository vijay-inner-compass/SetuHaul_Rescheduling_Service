"""Coordinator queue view and a minimal CLI.

    python -m setuhaul.coordinator --db data/working.sqlite3 list
    python -m setuhaul.coordinator --db ... show ESC-1234ABCD
    python -m setuhaul.coordinator --db ... claim ESC-1234ABCD --as asha
    python -m setuhaul.coordinator --db ... resolve ESC-1234ABCD --as asha --code ADVISED_DRIVER --note "..."
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from setuhaul.clock import Clock, SystemClock
from setuhaul.db import IST, open_connection
from setuhaul.escalation import (
    CaseView,
    TransitionResult,
    case_events,
    claim_case,
    get_case,
    list_open_cases,
    reopen_case,
    resolve_case,
)


def _t(dt: object) -> str:
    return f"{dt.astimezone(IST):%Y-%m-%d %H:%M:%S}" if dt is not None else "-"  # type: ignore[attr-defined]


def render_queue(cases: Sequence[CaseView]) -> str:
    if not cases:
        return "No open cases."
    lines = [f"{'CASE':<13} {'STATUS':<8} {'REASON':<28} {'DRIVER':<7} {'SHIPMENT':<8} {'OPENED (IST)':<19} CLAIMED BY"]
    for c in cases:
        lines.append(f"{c.case_id:<13} {c.status:<8} {c.reason_code:<28} {c.driver_id:<7} {c.shipment_id or '-':<8} "
                     f"{_t(c.created_at):<19} {c.claimed_by or '-'}")
    return "\n".join(lines)


def render_case(conn: sqlite3.Connection, case: CaseView) -> str:
    """Everything a coordinator needs without opening anything else."""
    ctx = case.context
    out = [
        f"Case {case.case_id}  [{case.status}]  reason={case.reason_code}  source={case.source}",
        f"Driver {case.driver_id}  thread {case.thread_id}  shipment {case.shipment_id or '-'}  language {ctx.get('language', '-')}",
        f"Opened {_t(case.created_at)}   SLA due {_t(case.sla_due_at)}   claimed by {case.claimed_by or '-'}   reopened {case.reopened_count}x",
        f"Summary: {case.summary}",
        "Context:",
    ]
    for k in sorted(ctx):
        if k != "recent_turns":
            out.append(f"  {k}: {ctx[k]}")
    out.append("Recent conversation:")
    for t in ctx.get("recent_turns", []):
        out.append(f"  [{t['role']}] {t['text']}")
    if case.shipment_id:
        a = conn.execute(
            "select a.appointment_id, a.appointment_status, s.slot_start_ts, d.dock_code from appointments a "
            "join appointment_slots s using(slot_id) join docks d on d.dock_id=s.dock_id where a.shipment_id=? "
            "and a.is_current=1", (case.shipment_id,)).fetchone()
        out.append("Current appointment: " + (f"{a['appointment_id']} {a['appointment_status']} {a['slot_start_ts']} dock {a['dock_code']}" if a else "none"))
    out.append("History:")
    for e in case_events(conn, case.case_id):
        out.append(f"  {e['occurred_at']}  {e['event_type']:<17} {e['actor_type']}/{e['actor_id'] or '-'}  {e['note'] or ''}")
    if case.resolution_code:
        out.append(f"Resolution: {case.resolution_code} - {case.resolution_note}")
    return "\n".join(out)


def main(argv: Sequence[str] | None = None, clock: Clock | None = None, out: TextIO | None = None) -> int:
    ap = argparse.ArgumentParser(prog="setuhaul.coordinator")
    ap.add_argument("--db", type=Path, required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("show", "claim", "resolve", "reopen"):
        p = sub.add_parser(name)
        p.add_argument("case_id")
        if name != "show":
            p.add_argument("--as", dest="coordinator", required=True)
        if name == "resolve":
            p.add_argument("--code", required=True)
            p.add_argument("--note", required=True)
    args = ap.parse_args(argv)
    clk = clock or SystemClock()
    write = (out or sys.stdout).write
    conn = open_connection(path=args.db)
    try:
        if args.cmd == "list":
            write(render_queue(list_open_cases(conn)) + "\n")
            return 0
        if args.cmd == "show":
            case = get_case(conn, args.case_id)
            write((render_case(conn, case) if case else f"No such case {args.case_id}") + "\n")
            return 0 if case else 1
        result: TransitionResult
        if args.cmd == "claim":
            result = claim_case(conn, args.case_id, args.coordinator, clk)
        elif args.cmd == "resolve":
            result = resolve_case(conn, args.case_id, args.coordinator, args.code, args.note, clk)
        else:
            result = reopen_case(conn, args.case_id, args.coordinator, clk)
        write(f"{result.status}" + (f" (held by {result.claimed_by})" if result.claimed_by and result.status != "CLAIMED" else "") + "\n")
        return 0 if result.status in ("CLAIMED", "RESOLVED", "REOPENED") else 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
