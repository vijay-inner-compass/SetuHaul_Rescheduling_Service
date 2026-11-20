"""Coordinator queue view and a minimal CLI.

    python -m setuhaul.coordinator --db data/working.sqlite3 list
    python -m setuhaul.coordinator --db ... show ESC-1234ABCD
    python -m setuhaul.coordinator --db ... claim ESC-1234ABCD --as asha
    python -m setuhaul.coordinator --db ... resolve ESC-1234ABCD --as asha --code ADVISED_DRIVER --note "..."
"""

# Lazy annotations.
from __future__ import annotations

# argparse builds the command-line interface.
import argparse

# sqlite3 for the Connection type.
import sqlite3

# sys.stdout is the default output stream.
import sys

# Sequence is the read-only list type for arguments.
from collections.abc import Sequence

# Path for the --db argument.
from pathlib import Path

# TextIO is the type of an output stream (a file or stdout; tests pass a buffer).
from typing import TextIO

# Clocks: the real one for the CLI, a fake one in tests.
from setuhaul.clock import Clock, SystemClock

# IST for display; open_connection for the database.
from setuhaul.db import IST, open_connection

# The case queue operations this CLI wraps.
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
    # Format a timestamp in IST to the second, or "-" when absent. The type-ignore is because dt is typed as a plain object.
    return f"{dt.astimezone(IST):%Y-%m-%d %H:%M:%S}" if dt is not None else "-"  # type: ignore[attr-defined]


def render_queue(cases: Sequence[CaseView]) -> str:
    # An empty queue is a normal state, said plainly.
    if not cases:
        return "No open cases."
    # The header row. The numbers are the column widths so rows line up.
    lines = [f"{'CASE':<13} {'STATUS':<8} {'REASON':<28} {'DRIVER':<7} {'SHIPMENT':<8} {'OPENED (IST)':<19} CLAIMED BY"]
    # One line per case, using the same widths.
    for c in cases:
        lines.append(f"{c.case_id:<13} {c.status:<8} {c.reason_code:<28} {c.driver_id:<7} {c.shipment_id or '-':<8} "
                     f"{_t(c.created_at):<19} {c.claimed_by or '-'}")
    return "\n".join(lines)


def render_case(conn: sqlite3.Connection, case: CaseView) -> str:
    """Everything a coordinator needs without opening anything else."""
    # The context dictionary saved when the hand-off was created (language, recent turns, tools used ...).
    ctx = case.context
    # The header block: identity, timing, owner, and the one-line summary.
    out = [
        f"Case {case.case_id}  [{case.status}]  reason={case.reason_code}  source={case.source}",
        f"Driver {case.driver_id}  thread {case.thread_id}  shipment {case.shipment_id or '-'}  language {ctx.get('language', '-')}",
        f"Opened {_t(case.created_at)}   SLA due {_t(case.sla_due_at)}   claimed by {case.claimed_by or '-'}   reopened {case.reopened_count}x",
        f"Summary: {case.summary}",
        "Context:",
    ]
    # Every context key in alphabetical order, except the conversation which gets its own section below.
    for k in sorted(ctx):
        if k != "recent_turns":
            out.append(f"  {k}: {ctx[k]}")
    # The last few messages, so the coordinator sees what the driver actually said.
    out.append("Recent conversation:")
    for t in ctx.get("recent_turns", []):
        out.append(f"  [{t['role']}] {t['text']}")
    # If the case is tied to a shipment, show its current appointment so the coordinator knows what is booked right now.
    if case.shipment_id:
        a = conn.execute(
            "select a.appointment_id, a.appointment_status, s.slot_start_ts, d.dock_code from appointments a "
            "join appointment_slots s using(slot_id) join docks d on d.dock_id=s.dock_id where a.shipment_id=? "
            "and a.is_current=1", (case.shipment_id,)).fetchone()
        out.append("Current appointment: " + (f"{a['appointment_id']} {a['appointment_status']} {a['slot_start_ts']} dock {a['dock_code']}" if a else "none"))
    # The full history of the case: created, claimed, resolved, reopened ...
    out.append("History:")
    for e in case_events(conn, case.case_id):
        out.append(f"  {e['occurred_at']}  {e['event_type']:<17} {e['actor_type']}/{e['actor_id'] or '-'}  {e['note'] or ''}")
    # A resolved case also shows how it was resolved.
    if case.resolution_code:
        out.append(f"Resolution: {case.resolution_code} - {case.resolution_note}")
    return "\n".join(out)


def main(argv: Sequence[str] | None = None, clock: Clock | None = None, out: TextIO | None = None) -> int:
    # Define the command line: a required database path and one sub-command.
    ap = argparse.ArgumentParser(prog="setuhaul.coordinator")
    ap.add_argument("--db", type=Path, required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    # show, claim, resolve and reopen all take a case id.
    for name in ("show", "claim", "resolve", "reopen"):
        p = sub.add_parser(name)
        p.add_argument("case_id")
        # Everything except "show" changes state, so it must say who is acting (--as).
        if name != "show":
            p.add_argument("--as", dest="coordinator", required=True)
        # Resolving needs a machine-readable code and a human note.
        if name == "resolve":
            p.add_argument("--code", required=True)
            p.add_argument("--note", required=True)
    # Parse the arguments (argv is None in real use, so argparse reads sys.argv).
    args = ap.parse_args(argv)
    # Use the supplied clock (tests) or the real one.
    clk = clock or SystemClock()
    # Write to the supplied stream (tests) or stdout.
    write = (out or sys.stdout).write
    # One connection for the whole command.
    conn = open_connection(path=args.db)
    try:
        # "list": print the open queue.
        if args.cmd == "list":
            write(render_queue(list_open_cases(conn)) + "\n")
            return 0
        # "show": print one case in full; exit code 1 if it does not exist.
        if args.cmd == "show":
            case = get_case(conn, args.case_id)
            write((render_case(conn, case) if case else f"No such case {args.case_id}") + "\n")
            return 0 if case else 1
        # The remaining commands change a case and return a TransitionResult.
        result: TransitionResult
        if args.cmd == "claim":
            result = claim_case(conn, args.case_id, args.coordinator, clk)
        elif args.cmd == "resolve":
            result = resolve_case(conn, args.case_id, args.coordinator, args.code, args.note, clk)
        else:
            result = reopen_case(conn, args.case_id, args.coordinator, clk)
        # Print the outcome; if somebody else holds the case, name them.
        write(f"{result.status}" + (f" (held by {result.claimed_by})" if result.claimed_by and result.status != "CLAIMED" else "") + "\n")
        # Exit 0 on success, 2 on a refused transition (already claimed, not found, too late to reopen ...).
        return 0 if result.status in ("CLAIMED", "RESOLVED", "REOPENED") else 2
    finally:
        # Always close the connection.
        conn.close()


# Allow "python -m setuhaul.coordinator ..." to run main and use its return value as the exit code.
if __name__ == "__main__":
    raise SystemExit(main())
