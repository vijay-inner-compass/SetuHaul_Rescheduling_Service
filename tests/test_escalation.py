"""Phase 9: every hand-off has an owner and a lifecycle."""

from __future__ import annotations

import io
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from setuhaul.clock import FakeClock
from setuhaul.config import Settings
from setuhaul.coordinator import main as coordinator_main
from setuhaul.coordinator import render_case, render_queue
from setuhaul.db import make_working_db, open_connection
from setuhaul.escalation import (
    HandoffRequest,
    JsonlEscalationSink,
    SqlEscalationSink,
    cancel_case_by_driver,
    case_events,
    claim_case,
    create_handoff,
    get_case,
    get_driver_case_status,
    ingest_fallback_file,
    list_open_cases,
    reopen_case,
    resolve_case,
)
from setuhaul.identity import AuthError
from setuhaul.integrity import check_invariants
from tests.conv_helpers import Chat, say_text, tc
from tests.helpers import at, seed_everything_known


def req(clock: FakeClock, reason: str = "NO_SLOT", driver: str = "DRV006", thread: str = "THR001",
        shipment: str | None = "SHP1006", **ctx: object) -> HandoffRequest:
    return HandoffRequest(reason_code=reason, driver_id=driver, thread_id=thread, shipment_id=shipment,
                          summary="No slot fits", context={"language": "en", "eta": "11:20", **ctx}, created_at=clock.now())


def thread_status(db: sqlite3.Connection, thread: str) -> str:
    return str(db.execute("select thread_status from chat_threads where thread_id=?", (thread,)).fetchone()[0])


def events(db: sqlite3.Connection, case_id: str) -> list[str]:
    return [e["event_type"] for e in case_events(db, case_id)]


# --- creation ------------------------------------------------------------------------------------------
def test_handoff_creates_an_open_case_with_full_context_and_no_sla(db: sqlite3.Connection, clock: FakeClock) -> None:
    out = create_handoff(db, req(clock, rejections={"UNLOAD_EXCEEDS_SLOT": 70}), clock)
    case = get_case(db, out.case_id)
    assert out.created and case is not None and case.status == "OPEN" and case.sla_due_at is None
    assert case.context["rejections"] == {"UNLOAD_EXCEEDS_SLOT": 70} and case.reason_code == "NO_SLOT"
    assert events(db, out.case_id) == ["CREATED"] and thread_status(db, "THR001") == "ESCALATED"


def test_duplicate_handoff_folds_into_the_active_case(db: sqlite3.Connection, clock: FakeClock) -> None:
    first = create_handoff(db, req(clock), clock)
    again = create_handoff(db, req(clock), clock)
    assert again.case_id == first.case_id and again.created is False
    assert events(db, first.case_id) == ["CREATED", "DUPLICATE_REQUEST"]
    assert db.execute("select count(*) from escalation_cases").fetchone()[0] == 1
    other_reason = create_handoff(db, req(clock, reason="SAFETY"), clock)
    other_shipment = create_handoff(db, req(clock, shipment="SHP1021"), clock)
    assert len({first.case_id, other_reason.case_id, other_shipment.case_id}) == 3


def test_every_trigger_reason_is_accepted_and_unknown_ones_are_not(clock: FakeClock) -> None:
    from setuhaul.escalation import REASON_CODES

    assert {"NO_SLOT", "CONTRADICTORY_DATA", "INCOMPLETE_FEASIBILITY_DATA", "SAFETY", "REGULATED_LOAD", "POLICY_CONFLICT",
            "CUSTOMER_COMMITMENT", "APPROVAL_REQUIRED", "DATABASE_UNAVAILABLE", "MODEL_UNAVAILABLE", "LANGUAGE_UNSUPPORTED"} <= REASON_CODES
    with pytest.raises(ValueError):
        req(clock, reason="BECAUSE")


def test_the_conversation_creates_a_queue_case_and_replies_with_its_reference(db: sqlite3.Connection, clock: FakeClock, tmp_path: Path) -> None:
    seed_everything_known(db)
    chat = Chat(db, clock, tmp_path, driver="DRV014", thread="THR004")
    chat.sink = SqlEscalationSink(db, clock)  # type: ignore[assignment]
    r = chat.say("SHP1014 is 70 minutes late", [tc("search_slot_options", shipment_id="SHP1014", report={"kind": "late_by", "minutes": 70}), say_text("x")])
    case = list_open_cases(db)[0]
    assert r.case_id == case.case_id and case.case_id in r.text and case.reason_code == "NO_SLOT"
    assert case.context["language"] == "en" and "recent_turns" in case.context
    status = get_driver_case_status(db, "DRV014", "THR004")
    assert status is not None and status.status == "OPEN" and case.case_id in status.text


# --- queue and claiming ------------------------------------------------------------------------------------
def test_queue_lists_oldest_first_with_filters_and_limit(db: sqlite3.Connection, clock: FakeClock) -> None:
    a = create_handoff(db, req(clock), clock).case_id
    clock.advance(timedelta(minutes=1))
    b = create_handoff(db, req(clock, driver="DRV012", thread="THR002", shipment="SHP1012"), clock).case_id
    clock.advance(timedelta(minutes=1))
    c = create_handoff(db, req(clock, reason="SAFETY"), clock).case_id
    claim_case(db, b, "asha", clock)
    assert [x.case_id for x in list_open_cases(db)] == [a, b, c]
    assert [x.case_id for x in list_open_cases(db, statuses=("OPEN",))] == [a, c]
    assert [x.case_id for x in list_open_cases(db, limit=1)] == [a]


def test_two_coordinators_claiming_one_case(db: sqlite3.Connection, clock: FakeClock) -> None:
    cid = create_handoff(db, req(clock), clock).case_id
    first, second = claim_case(db, cid, "asha", clock), claim_case(db, cid, "ben", clock)
    assert first.status == "CLAIMED" and second.status == "ALREADY_CLAIMED" and second.claimed_by == "asha"
    assert get_case(db, cid).claimed_by == "asha"  # type: ignore[union-attr]
    again = claim_case(db, cid, "asha", clock)
    assert again.status == "ALREADY_CLAIMED" and again.replayed is True
    assert events(db, cid) == ["CREATED", "CLAIMED"]
    assert claim_case(db, "ESC-NOPE", "asha", clock).status == "NOT_FOUND"


def test_racing_coordinators_in_separate_connections_produce_one_owner(tmp_path: Path, clock: FakeClock) -> None:
    path = make_working_db(tmp_path / "q.sqlite3")
    setup = open_connection(path=path)
    cid = create_handoff(setup, req(clock), clock).case_id
    setup.close()
    results: list[tuple[str, str]] = []
    barrier = threading.Barrier(4)

    def worker(name: str) -> None:
        conn = open_connection(path=path)
        barrier.wait()
        results.append((name, claim_case(conn, cid, name, clock).status))
        conn.close()

    ts = [threading.Thread(target=worker, args=(n,)) for n in ("asha", "ben", "chitra", "dev")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(s for _, s in results) == ["ALREADY_CLAIMED"] * 3 + ["CLAIMED"]
    winner = next(n for n, s in results if s == "CLAIMED")
    check = open_connection(path=path)
    assert get_case(check, cid).claimed_by == winner  # type: ignore[union-attr]
    assert check_invariants(check) == []
    check.close()


# --- resolution ----------------------------------------------------------------------------------------------
def test_only_the_claiming_coordinator_resolves_and_the_driver_is_told_in_the_chat(db: sqlite3.Connection, clock: FakeClock) -> None:
    cid = create_handoff(db, req(clock), clock).case_id
    assert resolve_case(db, cid, "asha", "ADVISED_DRIVER", "x", clock).status == "NOT_CLAIMED"
    claim_case(db, cid, "asha", clock)
    assert resolve_case(db, cid, "ben", "ADVISED_DRIVER", "x", clock).status == "NOT_YOUR_CASE"
    assert resolve_case(db, cid, "asha", "SOMETHING", "x", clock).status == "INVALID_RESOLUTION"
    ok = resolve_case(db, cid, "asha", "ASSIGNED_SLOT", "We booked dock D2 at 13:00 for you.", clock)
    assert ok.status == "RESOLVED"
    case = get_case(db, cid)
    assert (case.status, case.resolution_code) == ("RESOLVED", "ASSIGNED_SLOT") and case.resolved_at == clock.now()  # type: ignore[union-attr]
    texts = [r[0] for r in db.execute("select message_text from chat_messages where thread_id='THR001' and sender_type='OPERATIONS' order by rowid")]
    assert any("looking at your case" in t for t in texts) and texts[-1] == f"Operations has resolved your case {cid}: We booked dock D2 at 13:00 for you."
    assert thread_status(db, "THR001") == "OPEN"
    assert resolve_case(db, cid, "asha", "ASSIGNED_SLOT", "x", clock).status == "ALREADY_FINISHED"
    assert events(db, cid) == ["CREATED", "CLAIMED", "RESOLVED"]


def test_open_to_claimed_to_resolved_replay_leaves_a_clean_database(db: sqlite3.Connection, clock: FakeClock) -> None:
    cid = create_handoff(db, req(clock), clock).case_id
    for step in range(3):
        clock.advance(timedelta(minutes=2))
        [lambda: claim_case(db, cid, "asha", clock), lambda: resolve_case(db, cid, "asha", "ADVISED_DRIVER", "ok", clock),
         lambda: get_case(db, cid)][step]()
    stamps = [e["occurred_at"] for e in case_events(db, cid)]
    assert stamps == sorted(stamps) and check_invariants(db) == []


# --- reopen ----------------------------------------------------------------------------------------------------
def test_reopen_inside_the_window_and_a_new_case_after_it(db: sqlite3.Connection, clock: FakeClock) -> None:
    cid = create_handoff(db, req(clock), clock).case_id
    claim_case(db, cid, "asha", clock)
    resolve_case(db, cid, "asha", "ADVISED_DRIVER", "ok", clock)
    clock.advance(timedelta(hours=3))
    assert reopen_case(db, cid, "ben", clock).status == "REOPENED"
    case = get_case(db, cid)
    assert (case.status, case.reopened_count, case.claimed_by, case.resolution_code) == ("OPEN", 1, None, None)  # type: ignore[union-attr]
    claim_case(db, cid, "ben", clock)
    resolve_case(db, cid, "ben", "ADVISED_DRIVER", "ok again", clock)
    clock.advance(timedelta(hours=25))
    assert reopen_case(db, cid, "ben", clock).status == "REOPEN_WINDOW_PASSED"
    fresh = create_handoff(db, req(clock), clock)
    assert fresh.created and fresh.case_id != cid
    assert reopen_case(db, cid, "ben", clock).status == "REOPEN_WINDOW_PASSED"


def test_only_a_resolved_case_can_be_reopened_and_not_over_a_newer_active_one(db: sqlite3.Connection, clock: FakeClock) -> None:
    cid = create_handoff(db, req(clock), clock).case_id
    assert reopen_case(db, cid, "asha", clock).status == "NOT_RESOLVED"
    claim_case(db, cid, "asha", clock)
    resolve_case(db, cid, "asha", "NO_ACTION_NEEDED", "ok", clock)
    newer = create_handoff(db, req(clock), clock)
    assert newer.created and reopen_case(db, cid, "asha", clock).status == "NOT_RESOLVED"


# --- driver cancellation ----------------------------------------------------------------------------------------
def test_driver_can_cancel_an_open_or_claimed_case_and_nobody_else_can(db: sqlite3.Connection, clock: FakeClock) -> None:
    cid = create_handoff(db, req(clock), clock).case_id
    with pytest.raises(AuthError) as err:
        cancel_case_by_driver(db, cid, "DRV007", clock)
    assert err.value.code == "FOREIGN_CASE"
    claim_case(db, cid, "asha", clock)
    out = cancel_case_by_driver(db, cid, "DRV006", clock)
    assert out.status == "CANCELLED" and out.claimed_by == "asha"
    e = case_events(db, cid)[-1]
    assert (e["event_type"], e["actor_type"], e["note"]) == ("CANCELLED", "DRIVER", "driver cancelled while claimed by asha")
    assert claim_case(db, cid, "ben", clock).status == "NOT_OPEN"
    assert cancel_case_by_driver(db, cid, "DRV006", clock).status == "ALREADY_FINISHED"
    assert thread_status(db, "THR001") == "OPEN"


# --- what the driver is told -------------------------------------------------------------------------------------
@pytest.mark.parametrize(("lang", "needle"), [("en", "Updates will appear here in this chat"), ("hi", "अपडेट इसी चैट में दिखेंगे"),
                                              ("hinglish", "Updates isi chat mein dikhenge")])
def test_driver_status_says_received_and_where_updates_arrive_without_a_time(db: sqlite3.Connection, clock: FakeClock, lang: str, needle: str) -> None:
    cid = create_handoff(db, req(clock, language=lang), clock).case_id
    s = get_driver_case_status(db, "DRV006")
    assert s is not None and cid in s.text and needle in s.text and s.sla_minutes is None
    assert "minute" not in s.text.lower() and "hour" not in s.text.lower() and "मिनट" not in s.text
    claim_case(db, cid, "asha", clock)
    s2 = get_driver_case_status(db, "DRV006")
    assert s2 is not None and s2.status == "CLAIMED" and cid in s2.text


def test_a_response_time_is_only_promised_when_an_sla_is_approved(db: sqlite3.Connection, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    import setuhaul.escalation as esc

    monkeypatch.setattr(esc, "load_settings", lambda: Settings(db_path=Path("x"), escalation_sla_minutes=30))
    cid = create_handoff(db, req(clock), clock).case_id
    case = get_case(db, cid)
    assert case is not None and case.sla_due_at == clock.now() + timedelta(minutes=30)
    s = get_driver_case_status(db, "DRV006")
    assert s is not None and s.sla_minutes == 30 and "within 30 minutes" in s.text


def test_no_case_means_no_status(db: sqlite3.Connection) -> None:
    assert get_driver_case_status(db, "DRV006") is None


# --- audit trail is immutable ------------------------------------------------------------------------------------
def test_case_events_cannot_be_changed_or_deleted(db: sqlite3.Connection, clock: FakeClock) -> None:
    create_handoff(db, req(clock), clock)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("update escalation_events set note='x'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("delete from escalation_events")
    db.rollback()


def test_the_database_refuses_inconsistent_case_rows(db: sqlite3.Connection, clock: FakeClock) -> None:
    cid = create_handoff(db, req(clock), clock).case_id
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("update escalation_cases set status='CLAIMED' where case_id=?", (cid,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("update escalation_cases set status='RESOLVED' where case_id=?", (cid,))
    db.rollback()


# --- hand-offs written while the database was down ------------------------------------------------------------------
def test_fallback_file_hand_offs_are_ingested_once(db: sqlite3.Connection, clock: FakeClock, tmp_path: Path) -> None:
    f = tmp_path / "fallback.jsonl"
    sink = JsonlEscalationSink(f)
    sink.record(req(clock, reason="DATABASE_UNAVAILABLE", message_id="M-1"))
    sink.record(req(clock, reason="DATABASE_UNAVAILABLE", message_id="M-2"))  # same key: folds into one case
    sink.record(req(clock, reason="MODEL_UNAVAILABLE", driver="DRV012", thread="THR002", shipment=None))
    assert ingest_fallback_file(db, f, clock) == 2
    assert not f.exists() and (tmp_path / "fallback.jsonl.ingested").exists()
    assert {c.source for c in list_open_cases(db)} == {"FALLBACK_FILE"}
    assert ingest_fallback_file(db, f, clock) == 0


# --- the coordinator view ------------------------------------------------------------------------------------------
def test_case_view_has_everything_a_coordinator_needs(db: sqlite3.Connection, clock: FakeClock) -> None:
    cid = create_handoff(db, req(clock, recent_turns=[{"role": "user", "text": "SHP1006 is 60 minutes late"}],
                                 rejections={"SLOT_OCCUPIED": 13}), clock).case_id
    claim_case(db, cid, "asha", clock)
    text = render_case(db, get_case(db, cid))  # type: ignore[arg-type]
    for needle in (cid, "NO_SLOT", "DRV006", "SHP1006", "SLOT_OCCUPIED", "SHP1006 is 60 minutes late", "APT1006", "CLAIMED", "asha", "History"):
        assert needle in text, needle
    assert cid in render_queue(list_open_cases(db)) and render_queue([]) == "No open cases."


def test_cli_lists_shows_claims_and_resolves(tmp_path: Path, clock: FakeClock) -> None:
    path = make_working_db(tmp_path / "cli.sqlite3")
    conn = open_connection(path=path)
    cid = create_handoff(conn, req(clock), clock).case_id
    conn.close()

    def run(*args: str) -> tuple[int, str]:
        buf = io.StringIO()
        rc = coordinator_main(["--db", str(path), *args], clock=clock, out=buf)
        return rc, buf.getvalue()

    rc, out = run("list")
    assert rc == 0 and cid in out and "NO_SLOT" in out
    assert run("claim", cid, "--as", "asha")[0] == 0
    rc, out = run("claim", cid, "--as", "ben")
    assert rc == 2 and "ALREADY_CLAIMED (held by asha)" in out
    assert run("resolve", cid, "--as", "ben", "--code", "ADVISED_DRIVER", "--note", "x")[0] == 2
    assert run("resolve", cid, "--as", "asha", "--code", "ADVISED_DRIVER", "--note", "done")[0] == 0
    rc, out = run("show", cid)
    assert rc == 0 and "RESOLVED" in out and "Resolution: ADVISED_DRIVER - done" in out
    assert run("show", "ESC-NOPE")[0] == 1


def test_at_helper_still_consistent() -> None:
    assert at("09:35").hour == 9
