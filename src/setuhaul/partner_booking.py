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
from setuhaul import chat_loads, passes
from setuhaul.clock import Clock

# immediate() takes the write lock up front.
from setuhaul.db import immediate

# Bad replies become escalation cases for operations.
from setuhaul.escalation import HandoffRequest, create_handoff

# History rows are append-only.
from setuhaul.events import add_driver_notice, append_appointment_event

# Wording shared with the rest of the service.
from setuhaul.language import say

# Shipments in these states can no longer be moved.
_NOT_RESCHEDULABLE = ("CANCELLED", "COMPLETED", "IN_DOCK")


class PartnerRequestResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["REQUESTED", "ALREADY_PENDING", "NOT_PARTNER_FACILITY", "NO_PARTNER_CONTACT",
                    "SHIPMENT_NOT_RESCHEDULABLE", "START_IN_THE_PAST"]
    request_ref: str | None = None


def _new_ref() -> str:
    # PR- plus 8 uppercase hex characters: short enough for a subject line, unique enough per request.
    return "PR-" + uuid.uuid4().hex[:8].upper()


def request_slot(conn: sqlite3.Connection, clock: Clock, *, shipment_id: str, proposed_start: datetime,
                 proposed_end: datetime, ttl_min: int = 120) -> PartnerRequestResult:
    """Record one pending request for a shipment bound for a partner facility. The email is sent later by a job."""
    # A slot that has already started cannot be asked for: an approval would book the past and cancel the real appointment.
    if proposed_start <= clock.now():
        return PartnerRequestResult(status="START_IN_THE_PAST")
    with immediate(conn):
        ship = conn.execute("select destination_facility_id, current_status from v_driver_shipments where shipment_id=?",
                            (shipment_id,)).fetchone()
        if ship is None or ship["current_status"] in _NOT_RESCHEDULABLE or passes.on_site(conn, shipment_id):
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
        due = conn.execute("select * from partner_requests where status='PENDING_PARTNER' and expires_at<=?", (now.isoformat(),)).fetchall()
        conn.execute("update partner_requests set status='EXPIRED', decided_at=? where status='PENDING_PARTNER' and expires_at<=?",
                     (now.isoformat(), now.isoformat()))
        for req in due:
            _tell_driver(conn, req, "EXPIRED", now)
        return len(due)


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
# What the driver sees
# ---------------------------------------------------------------------------------------------------------------------

_STATUS_KEY = {"PENDING_PARTNER": "partner_pending", "APPROVED": "partner_approved", "DECLINED": "partner_declined",
               "EXPIRED": "partner_expired", "FAILED": "partner_failed"}


def _say_status(status: str, facility: str, proposed_start: str, lang: str) -> str:
    start = datetime.fromisoformat(proposed_start)
    return say(_STATUS_KEY[status], lang, facility=facility, time=f"{start:%H:%M}", date=f"{start:%d %B}")


def render_partner_status(conn: sqlite3.Connection, shipment_id: str, lang: str = "en") -> str | None:
    """Wording for the shipment's latest partner request, taken from the stored state. None when there is none."""
    row = conn.execute("select r.status, r.proposed_start, f.facility_name from partner_requests r "
                       "join facilities f using(facility_id) where r.shipment_id=? order by r.created_at desc, r.rowid desc limit 1",
                       (shipment_id,)).fetchone()
    return None if row is None else _say_status(row["status"], row["facility_name"], row["proposed_start"], lang)


def _driver_thread(conn: sqlite3.Connection, shipment_id: str) -> str | None:
    """The chat thread to talk to the driver in: this shipment's newest, else the driver's newest."""
    row = conn.execute("select t.thread_id from chat_threads t join shipments s on s.driver_id=t.driver_id where s.shipment_id=? "
                       "order by (t.shipment_id=s.shipment_id) desc, t.opened_at desc limit 1", (shipment_id,)).fetchone()
    return None if row is None else str(row["thread_id"])


def _tell_driver(conn: sqlite3.Connection, req: sqlite3.Row, status: str, now: datetime) -> None:
    """Post the new state into the driver's chat and queue it for their phone, in their language. Caller owns the
    transaction, so the message and the change it reports are saved together or not at all."""
    driver_id = conn.execute("select driver_id from shipments where shipment_id=?", (req["shipment_id"],)).fetchone()[0]
    fac = conn.execute("select facility_name from facilities where facility_id=?", (req["facility_id"],)).fetchone()
    text = _say_status(status, fac["facility_name"], req["proposed_start"], passes.driver_language(conn, driver_id))
    thread = _driver_thread(conn, req["shipment_id"])
    if thread is not None:
        with chat_loads.recording(conn, thread, req["shipment_id"]):
            conn.execute("insert into chat_messages (chat_message_id, thread_id, sender_type, message_text, message_ts) "
                         "values (?,?, 'OPERATIONS', ?, ?)", ("MSG-" + uuid.uuid4().hex[:12].upper(), thread, text, now.isoformat()))
    add_driver_notice(conn, driver_id, req["shipment_id"], text, now)


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
# Only a bare decision counts: the email asks for "one word on the first line", so that is all we trust.
_APPROVE_WORDS = frozenset({"approve", "approved"})
_DECLINE_WORDS = frozenset({"decline", "declined", "reject", "rejected"})
# Pleasantries that may follow the decision word without changing its meaning.
_POLITE_WORDS = frozenset({"thanks", "thank", "you", "regards", "please", "ok", "okay", "sir", "madam"})


def parse_decision(body: str) -> Literal["APPROVE", "DECLINE", "AMBIGUOUS"]:
    """Deterministic reading of a reply: the first line of the new text must be APPROVE or DECLINE, nothing else.

    Quoted earlier mail is ignored. Conditions, refusals ("can't approve", "approve nahi"), questions, other words and
    anything in a later line or signature are AMBIGUOUS, so they can never book a slot.
    """
    first = ""
    for line in body.replace("\u2019", "'").splitlines():
        text = line.strip()
        if not text:
            continue
        if _QUOTE_RE.match(text):
            break
        first = text
        break
    # Letters only: any other character ("?", "-", a second sentence's punctuation) is handled by the word check below.
    if re.search(r"[?:;()\[\]=<>/\\-]", first.rstrip(".!, ")):
        return "AMBIGUOUS"
    words = re.findall(r"[a-z']+", first.lower())
    if not words or words[0] not in _APPROVE_WORDS | _DECLINE_WORDS:
        return "AMBIGUOUS"
    if any(w not in _POLITE_WORDS for w in words[1:]):
        return "AMBIGUOUS"
    return "APPROVE" if words[0] in _APPROVE_WORDS else "DECLINE"


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


def _authenticated(auth: str | None, from_addr: str, serv_id: str) -> bool:
    """Live mode: the receiving server must vouch for the sender (DMARC pass, or a DKIM pass for the sender's own domain)."""
    if not auth:
        return False
    parts = [p.strip().lower() for p in auth.split(";")]
    if parts[0] != serv_id.lower():
        return False
    results = " ".join(parts[1:])
    if "dmarc=pass" in results:
        return True
    signed = re.search(r"dkim=pass[^;]*?header\.(?:d|i)=@?([a-z0-9.-]+)", results)
    domain = parseaddr(from_addr)[1].rsplit("@", 1)[-1].lower()
    return bool(signed and domain and (signed.group(1) == domain or signed.group(1).endswith("." + domain)
                                       or domain.endswith("." + signed.group(1))))


def handle_reply(conn: sqlite3.Connection, clock: Clock, *, message_id: str, from_addr: str, subject: str,
                 in_reply_to: str | None, body: str, authentication_results: str | None, require_auth: bool = False,
                 auth_serv_id: str = "mx.google.com") -> ReplyOutcome:
    """Act on one partner reply email. Books only for a matched, verified, timely, unambiguous APPROVE.

    ``require_auth`` is set for a real mailbox: the receiving server's Authentication-Results must vouch for the sender.
    """
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
    if require_auth and not _authenticated(authentication_results, from_addr, auth_serv_id):
        return _escalate(conn, clock, req, message_id, from_addr, "SENDER_NOT_AUTHENTICATED")
    decision = parse_decision(body)
    if req["status"] in ("APPROVED", "DECLINED"):
        # The same decision again is harmless; a different one (a withdrawn approval, say) needs a person.
        if decision == ("APPROVE" if req["status"] == "APPROVED" else "DECLINE"):
            with immediate(conn):
                _record_inbound(conn, message_id, req["request_ref"], from_addr, "IGNORED_DUPLICATE", now)
            return ReplyOutcome(status="IGNORED_DUPLICATE")
        return _escalate(conn, clock, req, message_id, from_addr, "REPLY_AFTER_DECISION")
    if req["status"] != "PENDING_PARTNER":
        return _escalate(conn, clock, req, message_id, from_addr, "REQUEST_NOT_OPEN")
    if datetime.fromisoformat(req["expires_at"]) <= now:
        expire_partner_requests(conn, clock)
        return _escalate(conn, clock, req, message_id, from_addr, "REQUEST_EXPIRED")
    if decision == "AMBIGUOUS":
        return _escalate(conn, clock, req, message_id, from_addr, "AMBIGUOUS_REPLY")
    if decision == "DECLINE":
        with immediate(conn):
            live = conn.execute("select status from partner_requests where request_ref=?", (req["request_ref"],)).fetchone()
            if live["status"] != "PENDING_PARTNER" or not _record_inbound(conn, message_id, req["request_ref"], from_addr, "DECLINED", now):
                return ReplyOutcome(status="IGNORED_DUPLICATE")
            conn.execute("update partner_requests set status='DECLINED', reply_message_id=?, decided_at=? "
                         "where request_ref=? and status='PENDING_PARTNER'", (message_id, now.isoformat(), req["request_ref"]))
            _tell_driver(conn, req, "DECLINED", now)
        return ReplyOutcome(status="DECLINED")
    return _book(conn, clock, req, message_id, from_addr)


_ACTIVE_STATUSES = "('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS','HELD')"


def _pick_slot(conn: sqlite3.Connection, req: sqlite3.Row, ignore_appointment: str | None, stamp: str) -> str | None:
    """The slot to book at the partner: a dock with no overlapping active booking. Returns its slot id, or None if all are busy."""
    docks = conn.execute("select dock_id from docks where facility_id=? and dock_status='ACTIVE' order by dock_code",
                         (req["facility_id"],)).fetchall()
    for dock in docks:
        busy = conn.execute(
            "select 1 from appointments a join appointment_slots s using(slot_id) where s.dock_id=? "
            f"and a.appointment_status in {_ACTIVE_STATUSES} and s.slot_start_ts < ? and s.slot_end_ts > ? "
            "and a.appointment_id <> ? limit 1",
            (dock["dock_id"], req["proposed_end"], req["proposed_start"], ignore_appointment or "")).fetchone()
        if busy is not None:
            continue
        # A free slot with exactly this window (left by an earlier booking) is reused: the table allows one per dock and window.
        same = conn.execute("select slot_id, slot_status from appointment_slots where dock_id=? and slot_start_ts=? and slot_end_ts=?",
                            (dock["dock_id"], req["proposed_start"], req["proposed_end"])).fetchone()
        if same is not None:
            if same["slot_status"] == "OPEN":
                return str(same["slot_id"])
            continue
        slot_id = str("PSLOT-" + req["request_ref"])
        conn.execute("insert into appointment_slots (slot_id, facility_id, dock_id, slot_start_ts, slot_end_ts, slot_status, created_at) "
                     "values (?,?,?,?,?, 'OPEN', ?)", (slot_id, req["facility_id"], dock["dock_id"], req["proposed_start"],
                                                       req["proposed_end"], stamp))
        return slot_id
    return None


def _book(conn: sqlite3.Connection, clock: Clock, req: sqlite3.Row, message_id: str, from_addr: str) -> ReplyOutcome:
    """End the original appointment and create the partner booking in one transaction.

    Everything is re-read inside the write lock: the request row passed in may be stale (another worker, the expiry job).
    """
    ref, now = req["request_ref"], clock.now()
    stamp = now.isoformat()
    failed: ReplyOutcome | None = None
    appt_id = "APT-" + ref
    try:
        with immediate(conn):
            live = conn.execute("select status, expires_at from partner_requests where request_ref=?", (ref,)).fetchone()
            if conn.execute("select 1 from partner_inbound where message_id=?", (message_id,)).fetchone() or \
                    live["status"] in ("APPROVED", "DECLINED"):
                return ReplyOutcome(status="IGNORED_DUPLICATE")
            if live["status"] != "PENDING_PARTNER":
                failed = ReplyOutcome(status="ESCALATED", reason="REQUEST_NOT_OPEN")
            elif datetime.fromisoformat(live["expires_at"]) <= now:
                cur = conn.execute("update partner_requests set status='EXPIRED', decided_at=? where request_ref=? and status='PENDING_PARTNER'",
                             (stamp, ref))
                if cur.rowcount:
                    _tell_driver(conn, req, "EXPIRED", now)
                failed = ReplyOutcome(status="ESCALATED", reason="REQUEST_EXPIRED")
            else:
                ship = conn.execute("select current_status from shipments where shipment_id=?", (req["shipment_id"],)).fetchone()
                orig = conn.execute(
                    "select appointment_id, appointment_status, slot_id from appointments where shipment_id=? and is_current=1 "
                    "and appointment_status in ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')", (req["shipment_id"],)).fetchone()
                reason: str | None = None
                if ship is None or ship["current_status"] in _NOT_RESCHEDULABLE:
                    reason = "SHIPMENT_NOT_RESCHEDULABLE"
                elif orig is not None and orig["appointment_status"] == "IN_PROGRESS":
                    reason = "ORIGINAL_IN_PROGRESS"
                slot_id = None if reason else _pick_slot(conn, req, orig["appointment_id"] if orig else None, stamp)
                if reason is None and slot_id is None:
                    reason = "NO_FREE_DOCK"
                if reason is not None or slot_id is None:
                    # Nothing to book into, or the move is no longer possible: end the request, leave the original alone.
                    cur = conn.execute("update partner_requests set status='FAILED', reply_message_id=?, decided_at=? "
                                 "where request_ref=? and status='PENDING_PARTNER'", (message_id, stamp, ref))
                    if cur.rowcount:
                        _tell_driver(conn, req, "FAILED", now)
                    failed = ReplyOutcome(status="ESCALATED", reason=reason)
                else:
                    _record_inbound(conn, message_id, ref, from_addr, "BOOKED", now)
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
                                 "decided_at=? where request_ref=? and status='PENDING_PARTNER'", (slot_id, appt_id, message_id, stamp, ref))
                    _tell_driver(conn, req, "APPROVED", now)
    except sqlite3.IntegrityError:
        # A constraint stopped the booking (for example a concurrent writer took the slot): the transaction rolled back whole.
        with immediate(conn):
            cur = conn.execute("update partner_requests set status='FAILED', reply_message_id=?, decided_at=? "
                         "where request_ref=? and status='PENDING_PARTNER'", (message_id, stamp, ref))
            if cur.rowcount:
                _tell_driver(conn, req, "FAILED", now)
        return _escalate(conn, clock, req, message_id, from_addr, "SLOT_NO_LONGER_AVAILABLE")
    if failed is not None:
        return _escalate(conn, clock, req, message_id, from_addr, failed.reason or "BOOKING_FAILED")
    return ReplyOutcome(status="BOOKED", appointment_id=appt_id)
