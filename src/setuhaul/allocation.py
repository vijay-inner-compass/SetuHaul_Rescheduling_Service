"""Option ranking (per driver) and the simultaneous-request allocation policy.

Policy (decision D5/Q5): after hard eligibility checks, first-come-first-served at *successful hold
commit*. Priority never pre-empts an existing hold; it is advisory and only used for display or
escalation. Trade-off: a CRITICAL shipment can lose a slot to an earlier NORMAL one, and falls back
to its next option or a human hand-off. The authoritative commit lives in booking.acquire_hold
(Phase 5); ``allocate_fcfs`` is the pure statement of the rule used for policy tests.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from setuhaul.feasibility import FeasibilityDecision

ALLOCATION_POLICY = "FCFS_AT_HOLD_COMMIT"
DEFAULT_OPTION_COUNT = 3


class RankedOption(BaseModel):
    model_config = ConfigDict(frozen=True)

    rank: int
    decision: FeasibilityDecision


def rank_options(
    decisions: Sequence[FeasibilityDecision], limit: int = DEFAULT_OPTION_COUNT
) -> list[RankedOption]:
    """Deterministic order: earliest start, then dock code, then slot id. Only FEASIBLE rows."""
    eligible = [d for d in decisions if d.status == "FEASIBLE"]
    eligible.sort(key=lambda d: (d.slot_start, d.dock_code, d.slot_id))
    return [RankedOption(rank=i, decision=d) for i, d in enumerate(eligible[:limit], start=1)]


def allocate_fcfs(
    requests_in_commit_order: Sequence[tuple[str, Sequence[RankedOption]]],
) -> dict[str, str | None]:
    """Each request, in commit order, takes its best still-free option; None if all are gone."""
    taken: set[str] = set()
    out: dict[str, str | None] = {}
    for shipment_id, options in requests_in_commit_order:
        choice = next((o.decision.slot_id for o in options if o.decision.slot_id not in taken), None)
        if choice is not None:
            taken.add(choice)
        out[shipment_id] = choice
    return out
