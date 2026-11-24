"""Partner-warehouse slot requests: ask a partner by email, book only after their verified approval reply."""

# Lazy annotations.
from __future__ import annotations

# re for the request reference and reply parsing.
import re

# sqlite3 for the Connection type.
import sqlite3

# uuid for the short request reference.
import uuid

# Callable types the test fault hook.
from collections.abc import Callable

# datetime arithmetic for expiry.
from datetime import datetime, timedelta

# Literal types the result status.
# parseaddr turns '"Name" <a@b>' into the bare address.
from email.utils import parseaddr
from typing import Literal

# pydantic value objects, like the rest of the service layer.
from pydantic import BaseModel, ConfigDict

# The injectable clock.
from setuhaul.clock import Clock

# immediate() takes the write lock up front.
from setuhaul.db import immediate

# Bad replies become escalation cases for operations.
from setuhaul.escalation import HandoffRequest, create_handoff

# History rows are append-only.
from setuhaul.events import append_appointment_event

# Shipments in these states can no longer be moved.
_NOT_RESCHEDULABLE = ("CANCELLED", "COMPLETED", "IN_DOCK")


class PartnerRequestResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["REQUESTED", "ALREADY_PENDING", "NOT_PARTNER_FACILITY", "NO_PARTNER_CONTACT",
                    "SHIPMENT_NOT_RESCHEDULABLE"]
    request_ref: str | None = None


def _new_ref() -> str:
    # PR- plus 8 uppercase hex characters: short enough for a subject line, unique enough per request.
    return "PR-" + uuid.uuid4().hex[:8].upper()


def request_slot(conn: sqlite3.Connection, clock: Clock, *, shipment_id: str, proposed_start: datetime,
                 proposed_end: datetime, ttl_min: int = 120) -> PartnerRequestResult:
    """Record one pending request for a shipment bound for a partner facility. The email is sent later by a job."""
    with immediate(conn):
        ship = conn.execute("select destination_facility_id, current_status from shipments where shipment_id=?",
                            (shipment_id,)).fetchone()
        if ship is None or ship["current_status"] in _NOT_RESCHEDULABLE:
            return PartnerRequestResult(status="SHIPMENT_NOT_RESCHEDULABLE")
        # Only partner facilities are asked by email; our own docks go through the normal hold flow.
        fac = conn.execute("select is_partner from facilities where facility_id=?",
                           (ship["destination_facility_id"],)).fetchone()
        if fac is None or not fac["is_partner"]:
            return PartnerRequestResult(status="NOT_PARTNER_FACILITY")
        # One open request per shipment: a repeat returns the existing one.
        open_ = conn.execute("select request_ref from partner_requests where shipment_id=? and status='PENDING_PARTNER'",
                             (shipment_id,)).fetchone()
        if open_ is not None:
            return PartnerRequestResult(status="ALREADY_PENDING", request_ref=open_["request_ref"])
        # The coordinator first, then any other active contact that has an email address.
        contact = conn.execute(
            "select contact_id from facility_contacts where facility_id=? and active_flag=1 and coalesce(email,'')<>'' "
            "order by contact_role='WAREHOUSE_COORDINATOR' desc, contact_id limit 1", (ship["destination_facility_id"],)).fetchone()
        if contact is None:
            return PartnerRequestResult(status="NO_PARTNER_CONTACT")
        now = clock.now()
        ref = _new_ref()
        conn.execute(
            "insert into partner_requests (request_ref, shipment_id, facility_id, contact_id, proposed_start, proposed_end, "
            "status, email_status, email_attempts, created_at, expires_at) values (?,?,?,?,?,?,?,?,?,?,?)",
            (ref, shipment_id, ship["destination_facility_id"], contact["contact_id"], proposed_start.isoformat(),
             proposed_end.isoformat(), "PENDING_PARTNER", "PENDING", 0, now.isoformat(),
             (now + timedelta(minutes=ttl_min)).isoformat()))
        return PartnerRequestResult(status="REQUESTED", request_ref=ref)


def expire_partner_requests(conn: sqlite3.Connection, clock: Clock) -> int:
    """Mark overdue pending requests EXPIRED. The shipment's original appointment is never touched."""
    with immediate(conn):
        now = clock.now()
        cur = conn.execute(
            "update partner_requests set status='EXPIRED', decided_at=? where status='PENDING_PARTNER' and expires_at<=?",
            (now.isoformat(), now.isoformat()))
        return cur.rowcount


def render_request_email(conn: sqlite3.Connection, request_ref: str) -> tuple[str, str, str]:
    """(to_address, subject, body). No driver phone number or other personal detail goes in the body."""
    row = conn.execute(
        "select r.request_ref, r.proposed_start, r.proposed_end, c.email, c.contact_name, f.facility_name, "
        "s.order_reference from partner_requests r join facility_contacts c using(contact_id) "
        "join facilities f on f.facility_id=r.facility_id join shipments s using(shipment_id) where r.request_ref=?",
        (request_ref,)).fetchone()
    if row is None:
        raise KeyError(request_ref)
    start = datetime.fromisoformat(row["proposed_start"])
    end = datetime.fromisoformat(row["proposed_end"])
    subject = f"SetuHaul slot request [{request_ref}]"
    body = (
        f"Hello {row['contact_name']},\n\n"
        f"SetuHaul asks for a delivery slot at {row['facility_name']} for order {row['order_reference']}:\n"
        f"  {start:%A %d %B %Y}, {start:%H:%M} to {end:%H:%M} (India time)\n\n"
        "Please reply to this email with one word on the first line:\n"
        "  APPROVE   to confirm the slot\n"
        "  DECLINE   if it cannot be done\n\n"
        f"Keep [{request_ref}] in the subject. We book the slot only after your APPROVE reply.\n"
    )
    return row["email"], subject, body


# ---------------------------------------------------------------------------------------------------------------------
# Reply handling
# ---------------------------------------------------------------------------------------------------------------------

# Tests set this to raise at a named step, to prove a crash part-way through rolls the whole booking back.
fault_hook: Callable[[str], None] | None = None


def _fault(point: str) -> None:
    if fault_hook is not None:
        fault_hook(point)


class ReplyOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["BOOKED", "DECLINED", "IGNORED_DUPLICATE", "ESCALATED", "UNMATCHED"]
    reason: str | None = None
    appointment_id: str | None = None


_REF_RE = re.compile(r"\[(PR-[0-9A-F]{8})\]")
# Where quoted earlier mail starts: ">" lines, "On ... wrote:", Outlook-style separators.
_QUOTE_RE = re.compile(r"^(>|on .+wrote:\s*$|-{2,}\s*original message|from:\s)", re.IGNORECASE)
_APPROVE_RE = re.compile(r"\b(approve|approved)\b", re.IGNORECASE)
_DECLINE_RE = re.compile(r"\b(decline|declined|reject|rejected)\b", re.IGNORECASE)
# A negation right before the decision word flips its meaning, so the reply is treated as unclear.
_NEGATION_RE = re.compile(r"\b(not|no|never|cannot|can't|cant|don't|dont|unable|won't|wont)\W+(?:\w+\W+){0,2}$", re.IGNORECASE)


def parse_decision(body: str) -> Literal["APPROVE", "DECLINE", "AMBIGUOUS"]:
    """Strict, deterministic reading of a reply. Only the new text counts; quoted earlier mail is ignored."""
    fresh: list[str] = []
    for line in body.splitlines():
        if _QUOTE_RE.match(line.strip()):
            break
        fresh.append(line)
    text = "\n".join(fresh)
    approve, decline = _APPROVE_RE.search(text), _DECLINE_RE.search(text)
    # Both words, or neither: unclear.
    if bool(approve) == bool(decline):
        return "AMBIGUOUS"
    hit = approve or decline
    assert hit is not None
    if _NEGATION_RE.search(text[: hit.start()]):
        return "AMBIGUOUS"
    return "APPROVE" if approve else "DECLINE"


def _record_inbound(conn: sqlite3.Connection, message_id: str, ref: str | None, from_addr: str, outcome: str, now: datetime) -> bool:
    """Write the once-only inbound row. False means this message was already recorded."""
    cur = conn.execute("insert or ignore into partner_inbound (message_id, request_ref, from_addr, outcome, received_at) "
                       "values (?,?,?,?,?)", (message_id, ref, from_addr, outcome, now.isoformat()))
    return cur.rowcount == 1


def _match_request(conn: sqlite3.Connection, subject: str, in_reply_to: str | None) -> sqlite3.Row | None:
    found = _REF_RE.search(subject)
    if found:
        row: sqlite3.Row | None = conn.execute("select * from partner_requests where request_ref=?", (found.group(1),)).fetchone()
        return row
    if in_reply_to:
        by_id: sqlite3.Row | None = conn.execute("select * from partner_requests where message_id=?", (in_reply_to,)).fetchone()
        return by_id
    return None


def _sender_ok(conn: sqlite3.Connection, facility_id: str, from_addr: str) -> bool:
    addr = parseaddr(from_addr)[1].strip().lower()
    if not addr:
        return False
    rows = conn.execute("select email from facility_contacts where facility_id=? and active_flag=1", (facility_id,)).fetchall()
    return any((r["email"] or "").strip().lower() == addr for r in rows)


def _escalate(conn: sqlite3.Connection, clock: Clock, req: sqlite3.Row, message_id: str, from_addr: str, reason: str) -> ReplyOutcome:
    """Open a coordinator case for a reply we will not act on, then remember we saw the mail."""
    ship = conn.execute("select driver_id from shipments where shipment_id=?", (req["shipment_id"],)).fetchone()
    thread = conn.execute("select thread_id from chat_threads where driver_id=? order by (shipment_id=?) desc, opened_at desc limit 1",
                          (ship["driver_id"], req["shipment_id"])).fetchone()
    now = clock.now()
    if thread is None:
        # A driver with no chat yet still needs a thread to hang the case on.
        thread_id = "THR-" + req["request_ref"]
        conn.execute("insert or ignore into chat_threads (thread_id, driver_id, shipment_id, opened_at, thread_status, thread_intent) "
                     "values (?,?,?,?, 'OPEN', 'GENERAL_QUESTION')", (thread_id, ship["driver_id"], req["shipment_id"], now.isoformat()))
        conn.commit()
    else:
        thread_id = thread["thread_id"]
    create_handoff(conn, HandoffRequest(
        reason_code="OTHER_UNCERTAINTY", driver_id=ship["driver_id"], thread_id=thread_id, shipment_id=req["shipment_id"],
        summary=f"Partner reply for {req['request_ref']} not acted on: {reason}",
        context={"partner_request": req["request_ref"], "reason": reason, "from": parseaddr(from_addr)[1]},
        created_at=now), clock, source="PARTNER_EMAIL")
    conn.commit()
    with immediate(conn):
        _record_inbound(conn, message_id, req["request_ref"], from_addr, "ESCALATED:" + reason, now)
    return ReplyOutcome(status="ESCALATED", reason=reason)


def handle_reply(conn: sqlite3.Connection, clock: Clock, *, message_id: str, from_addr: str, subject: str,
                 in_reply_to: str | None, body: str, authentication_results: str | None) -> ReplyOutcome:
    """Act on one partner reply email. Books only for a matched, verified, timely, unambiguous APPROVE."""
    now = clock.now()
    if conn.execute("select 1 from partner_inbound where message_id=?", (message_id,)).fetchone():
        return ReplyOutcome(status="IGNORED_DUPLICATE")
    req = _match_request(conn, subject, in_reply_to)
    if req is None:
        with immediate(conn):
            _record_inbound(conn, message_id, None, from_addr, "UNMATCHED", now)
        return ReplyOutcome(status="UNMATCHED", reason="NO_MATCHING_REQUEST")
    if not _sender_ok(conn, req["facility_id"], from_addr):
        return _escalate(conn, clock, req, message_id, from_addr, "SENDER_NOT_RECOGNISED")
    auth = (authentication_results or "").lower()
    if any(f"{m}=fail" in auth for m in ("spf", "dkim", "dmarc")):
        return _escalate(conn, clock, req, message_id, from_addr, "SENDER_AUTHENTICATION_FAILED")
    if req["status"] in ("APPROVED", "DECLINED"):
        with immediate(conn):
            _record_inbound(conn, message_id, req["request_ref"], from_addr, "IGNORED_DUPLICATE", now)
        return ReplyOutcome(status="IGNORED_DUPLICATE")
    if req["status"] != "PENDING_PARTNER":
        return _escalate(conn, clock, req, message_id, from_addr, "REQUEST_NOT_OPEN")
    if datetime.fromisoformat(req["expires_at"]) <= now:
        expire_partner_requests(conn, clock)
        return _escalate(conn, clock, req, message_id, from_addr, "REQUEST_EXPIRED")
    decision = parse_decision(body)
    if decision == "AMBIGUOUS":
        return _escalate(conn, clock, req, message_id, from_addr, "AMBIGUOUS_REPLY")
    if decision == "DECLINE":
        with immediate(conn):
            if not _record_inbound(conn, message_id, req["request_ref"], from_addr, "DECLINED", now):
                return ReplyOutcome(status="IGNORED_DUPLICATE")
            conn.execute("update partner_requests set status='DECLINED', reply_message_id=?, decided_at=? "
                         "where request_ref=? and status='PENDING_PARTNER'", (message_id, now.isoformat(), req["request_ref"]))
        return ReplyOutcome(status="DECLINED")
    return _book(conn, clock, req, message_id, from_addr)


def _book(conn: sqlite3.Connection, clock: Clock, req: sqlite3.Row, message_id: str, from_addr: str) -> ReplyOutcome:
    """End the original appointment and create the partner booking in one transaction."""
    ref, now = req["request_ref"], clock.now()
    stamp = now.isoformat()
    try:
        with immediate(conn):
            if not _record_inbound(conn, message_id, ref, from_addr, "BOOKED", now):
                return ReplyOutcome(status="IGNORED_DUPLICATE")
            dock = conn.execute("select dock_id from docks where facility_id=? and dock_status='ACTIVE' order by dock_code limit 1",
                                (req["facility_id"],)).fetchone()
            orig = conn.execute(
                "select appointment_id, appointment_status, slot_id from appointments where shipment_id=? and is_current=1 "
                "and appointment_status in ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')", (req["shipment_id"],)).fetchone()
            if dock is None or (orig is not None and orig["appointment_status"] == "IN_PROGRESS"):
                # Nothing to book into, or unloading already started: end the request without touching the original.
                conn.execute("update partner_requests set status='FAILED', reply_message_id=?, decided_at=? where request_ref=?",
                             (message_id, stamp, ref))
                conn.execute("delete from partner_inbound where message_id=?", (message_id,))
                reason = "NO_ACTIVE_DOCK" if dock is None else "ORIGINAL_IN_PROGRESS"
                failed: ReplyOutcome | None = ReplyOutcome(status="ESCALATED", reason=reason)
            else:
                failed = None
                slot_id, appt_id = "PSLOT-" + ref, "APT-" + ref
                _fault("BEFORE_END_ORIGINAL")
                if orig is not None:
                    conn.execute("update appointments set appointment_status='CANCELLED', is_current=0, cancelled_at=?, "
                                 "cancellation_reason='RESCHEDULED', updated_at=? where appointment_id=?",
                                 (stamp, stamp, orig["appointment_id"]))
                    append_appointment_event(conn, orig["appointment_id"], req["shipment_id"], "ORIGINAL_ENDED",
                                             from_status=orig["appointment_status"], to_status="CANCELLED", actor_type="WAREHOUSE",
                                             actor_id=req["contact_id"], reason="RESCHEDULED", occurred_at=now,
                                             details={"replaced_by": appt_id, "partner_request": ref})
                _fault("AFTER_END_ORIGINAL")
                conn.execute("insert or ignore into appointment_slots (slot_id, facility_id, dock_id, slot_start_ts, slot_end_ts, "
                             "slot_status, created_at) values (?,?,?,?,?, 'OPEN', ?)",
                             (slot_id, req["facility_id"], dock["dock_id"], req["proposed_start"], req["proposed_end"], stamp))
                conn.execute("insert into appointments (appointment_id, shipment_id, slot_id, appointment_status, booking_source, "
                             "is_current, booked_at, confirmed_at, replaced_appointment_id, warehouse_confirmation_ref, updated_at) "
                             "values (?,?,?, 'CONFIRMED', 'WAREHOUSE', 1, ?,?,?,?,?)",
                             (appt_id, req["shipment_id"], slot_id, stamp, stamp, orig["appointment_id"] if orig else None,
                              message_id, stamp))
                _fault("AFTER_NEW_APPOINTMENT")
                append_appointment_event(conn, appt_id, req["shipment_id"], "PARTNER_BOOKING_CONFIRMED", from_status=None,
                                         to_status="CONFIRMED", actor_type="WAREHOUSE", actor_id=req["contact_id"],
                                         reason="PARTNER_APPROVED", occurred_at=now,
                                         details={"partner_request": ref, "from_slot_id": orig["slot_id"] if orig else None,
                                                  "to_slot_id": slot_id})
                conn.execute("update partner_requests set status='APPROVED', slot_id=?, appointment_id=?, reply_message_id=?, "
                             "decided_at=? where request_ref=?", (slot_id, appt_id, message_id, stamp, ref))
    except sqlite3.IntegrityError:
        # The partner slot was taken (unique index) after the request was made: the transaction rolled back whole.
        with immediate(conn):
            conn.execute("update partner_requests set status='FAILED', reply_message_id=?, decided_at=? where request_ref=?",
                         (message_id, stamp, ref))
        return _escalate(conn, clock, req, message_id, from_addr, "SLOT_NO_LONGER_AVAILABLE")
    if failed is not None:
        return _escalate(conn, clock, req, message_id, from_addr, failed.reason or "BOOKING_FAILED")
    return ReplyOutcome(status="BOOKED", appointment_id=appt_id)
