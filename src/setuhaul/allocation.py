"""Option ranking (per driver) and the simultaneous-request allocation policy.

Policy (decision D5/Q5): after hard eligibility checks, first-come-first-served at *successful hold
commit*. Priority never pre-empts an existing hold; it is advisory and only used for display or
escalation. Trade-off: a CRITICAL shipment can lose a slot to an earlier NORMAL one, and falls back
to its next option or a human hand-off. The authoritative commit lives in booking.acquire_hold
(Phase 5); ``allocate_fcfs`` is the pure statement of the rule used for policy tests.
"""

# Lazy annotations.
from __future__ import annotations

# Sequence is the read-only list type used for inputs, so callers may pass lists or tuples.
from collections.abc import Sequence

# BaseModel gives validated data classes; ConfigDict lets us freeze them (immutable once built).
from pydantic import BaseModel, ConfigDict

# A FeasibilityDecision is the verdict on one slot for one shipment (feasible, or why not).
from setuhaul.feasibility import FeasibilityDecision

# The name of the allocation rule, recorded so reports say which policy was in force.
ALLOCATION_POLICY = "FCFS_AT_HOLD_COMMIT"
# How many options a driver is shown at once: three is enough choice without overwhelming a chat.
DEFAULT_OPTION_COUNT = 3


# One line of the menu: its position and the decision behind it.
class RankedOption(BaseModel):
    # frozen: once ranked, an option cannot be altered, so what was shown is what is stored.
    model_config = ConfigDict(frozen=True)

    # 1-based position the driver sees ("option 1").
    rank: int
    # The full feasibility verdict, including slot id, dock, times and any warnings.
    decision: FeasibilityDecision


def rank_options(
    decisions: Sequence[FeasibilityDecision], limit: int = DEFAULT_OPTION_COUNT
) -> list[RankedOption]:
    """Deterministic order: earliest start, then dock code, then slot id. Only FEASIBLE rows."""
    # Drop everything that is not feasible; a rejected or incomplete slot must never be offered.
    eligible = [d for d in decisions if d.status == "FEASIBLE"]
    # Sort by earliest start, then dock code, then slot id: the last two break ties so the order never varies between runs.
    eligible.sort(key=lambda d: (d.slot_start, d.dock_code, d.slot_id))
    # Keep the first `limit` and number them from 1.
    return [RankedOption(rank=i, decision=d) for i, d in enumerate(eligible[:limit], start=1)]


def allocate_fcfs(
    requests_in_commit_order: Sequence[tuple[str, Sequence[RankedOption]]],
) -> dict[str, str | None]:
    """Each request, in commit order, takes its best still-free option; None if all are gone."""
    # Slot ids already given to an earlier request in this pass.
    taken: set[str] = set()
    # Result: shipment id -> the slot it got, or None.
    out: dict[str, str | None] = {}
    # Requests arrive already ordered by who committed first; that order is the whole policy.
    for shipment_id, options in requests_in_commit_order:
        # Take the first option (best rank) whose slot nobody earlier has taken; None if every option is gone.
        choice = next((o.decision.slot_id for o in options if o.decision.slot_id not in taken), None)
        # Reserve it so later requests cannot get the same slot.
        if choice is not None:
            taken.add(choice)
        # Record the outcome, including None, so the caller can send that driver to a hand-off.
        out[shipment_id] = choice
    return out
