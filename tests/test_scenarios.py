"""Phase 12: the required scenario runs end to end, repeatably, and every named element is present."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.scenario_flow import Bundle, run_scenario


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Bundle:
    return run_scenario(tmp_path_factory.mktemp("scenario"))


def test_every_check_in_the_scenario_passes(bundle: Bundle) -> None:
    assert [c.name for c in bundle.checks if not c.ok] == []
    assert len(bundle.checks) >= 20


@pytest.mark.parametrize("needle", [
    "clarification", "ETA derived", "one slot is held, not booked", "stored option set compared", "three drivers hold stored option sets",
    "first valid request wins", "exactly one hold", "cross-driver read refused", "confirmation is state-backed", "lapsed hold",
    "cancellation outside the cutoff", "no-slot hand-off", "gate-in contradiction", "missing labour evidence", "coordinator lifecycle",
    "downstream notices queued", "notification failure left the booking intact", "history shows who changed what", "zero invariant violations",
])
def test_each_element_the_brief_names_is_covered(bundle: Bundle, needle: str) -> None:
    assert any(needle in c.name for c in bundle.checks), needle


def test_the_scenario_is_deterministic(bundle: Bundle, tmp_path: Path) -> None:
    again = run_scenario(tmp_path / "second")
    assert again.text() == bundle.text()
    assert again.scorecard == bundle.scorecard


def test_transcript_shows_system_worded_facts_and_no_run_specific_noise(bundle: Bundle) -> None:
    text = bundle.text()
    assert "Your appointment is confirmed: dock D2, 12:00-13:00 on 04 Aug 2026. Reference $HOLD1." in text
    assert "ORIGINAL_ENDED" in text and "RESCHEDULE_CONFIRMED" in text and "APPOINTMENT_CANCELLED" in text
    assert "attempts=2" in text  # a failed downstream delivery was retried
    assert not re.search(r"OPT-[0-9A-F]{12}|APT-H-[0-9A-F]{10}|ESC-[0-9A-F]{8}", text)


def test_scorecard_reports_numerators_and_denominators_and_claims_nothing_extra(bundle: Bundle) -> None:
    sc = bundle.scorecard
    assert sc["conflicting_allocations"]["numerator"] == 0
    assert sc["cross_driver_reads"] == {"attempted": 1, "refused": 1, "succeeded": 0}
    assert sc["state_backed_confirmations"]["numerator"] == sc["state_backed_confirmations"]["denominator"] == 1
    assert sc["complete_handoffs"]["numerator"] == sc["complete_handoffs"]["denominator"] == 3
    assert sc["evaluator_checks"]["passed"] == sc["evaluator_checks"]["total"] >= 50
    assert sc["resolved_without_a_person"]["denominator"] == sc["drivers"] == 6
