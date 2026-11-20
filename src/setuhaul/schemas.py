"""Domain contracts for ETA evidence. All timestamps must be timezone-aware."""

# Lazy annotations.
from __future__ import annotations

# datetime for the type of timestamps passed to the helper constructors.
from datetime import datetime

# StrEnum: enums whose members are also plain strings (easy to store and compare).
from enum import StrEnum

# Annotated attaches validation rules to a type; Literal restricts a value to listed strings; Self is "this class".
from typing import Annotated, Literal, Self

# Pydantic: AwareDatetime rejects naive datetimes; AfterValidator runs a function after validation;
# Field sets numeric bounds; model_validator checks rules that involve several fields at once.
from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, model_validator

# The operational timezone.
from setuhaul.db import IST


def _to_ist(value: datetime) -> datetime:
    # Whatever offset the input carried, convert to IST so all comparisons and displays agree.
    return value.astimezone(IST)


# A timestamp that MUST carry a timezone and is always converted to IST. Naive times cannot get in.
Aware = Annotated[AwareDatetime, AfterValidator(_to_ist)]
# How sure we are of an ETA.
Confidence = Literal["LOW", "MEDIUM", "HIGH"]
# The dock types a shipment may require ("ANY" means no restriction).
DockType = Literal["ANY", "STANDARD", "REEFER", "HEAVY"]


# Base class for all contracts here: frozen (immutable) and extra="forbid" (an unknown field is an error, not ignored).
class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# What an ETA was measured from.
class BaselineType(StrEnum):
    # The ETA in the original plan.
    PLANNED_ETA = "PLANNED_ETA"
    # The latest updated ETA on file.
    CURRENT_ETA = "CURRENT_ETA"
    # An arrival time the driver stated outright.
    DRIVER_DECLARED = "DRIVER_DECLARED"
    # Computed from repair duration plus drive time.
    REPAIR_COMPLETION = "REPAIR_COMPLETION"
    # The truck is physically at the gate.
    GATE_IN = "GATE_IN"


class EtaRecord(_Model):
    """An ETA the system already holds, with the record it came from."""

    record_id: str
    eta: Aware


# The three ways a driver can describe a delay.
class DelayKind(StrEnum):
    LATE_BY = "LATE_BY"
    REPAIR = "REPAIR"
    EXPLICIT_ETA = "EXPLICIT_ETA"


class DelayReport(_Model):
    kind: DelayKind
    confidence: Confidence = "MEDIUM"
    # Minutes of delay; ge=0 forbids negatives. Only meaningful for LATE_BY.
    delay_minutes: Annotated[int, Field(ge=0)] | None = None
    # What "late" is measured against; defaults to the planned ETA (decision D6).
    baseline: BaselineType = BaselineType.PLANNED_ETA
    # For REPAIR: how long the fix takes, and how long the drive afterwards takes.
    repair_minutes: Annotated[int, Field(ge=0)] | None = None
    travel_minutes: Annotated[int, Field(ge=0)] | None = None
    # When the repair began, if the driver said (otherwise "now" is used).
    repair_started_at: Aware | None = None
    # For EXPLICIT_ETA: the arrival time the driver gave.
    declared_eta: Aware | None = None

    # Convenience constructors, so callers do not have to remember which fields belong to which kind.
    @classmethod
    def late_by(
        cls,
        minutes: int,
        baseline: BaselineType = BaselineType.PLANNED_ETA,
        confidence: Confidence = "MEDIUM",
    ) -> Self:
        return cls(kind=DelayKind.LATE_BY, delay_minutes=minutes, baseline=baseline, confidence=confidence)

    @classmethod
    def repair(
        cls,
        repair_minutes: int,
        travel_minutes: int | None = None,
        started_at: datetime | None = None,
        confidence: Confidence = "MEDIUM",
    ) -> Self:
        return cls(
            kind=DelayKind.REPAIR,
            repair_minutes=repair_minutes,
            travel_minutes=travel_minutes,
            repair_started_at=started_at,
            confidence=confidence,
        )

    @classmethod
    def explicit(cls, eta: datetime, confidence: Confidence = "MEDIUM") -> Self:
        return cls(kind=DelayKind.EXPLICIT_ETA, declared_eta=eta, confidence=confidence)

    # Runs after all fields are set: each kind must carry its own required value.
    @model_validator(mode="after")
    def _kind_fields(self) -> Self:
        # For each kind, the one field that must not be missing.
        needed = {
            DelayKind.LATE_BY: self.delay_minutes,
            DelayKind.REPAIR: self.repair_minutes,
            DelayKind.EXPLICIT_ETA: self.declared_eta,
        }[self.kind]
        # A LATE_BY report with no minutes, for example, is meaningless; reject it at the boundary.
        if needed is None:
            raise ValueError(f"{self.kind} report is missing its required value")
        return self


# A usable ETA, with where it came from and how much to trust it.
class EtaEvidence(_Model):
    eta: Aware
    # Where the ETA came from: the driver's word or a gate check-in record.
    source: Literal["DRIVER_DECLARED", "GATE_CHECKIN"]
    confidence: Confidence
    baseline_type: BaselineType
    # The id of the planned/updated ETA row used as the baseline, for traceability.
    baseline_record_id: str | None = None


# "I need one more fact before I can work out the ETA", with the question to ask.
class ClarificationNeeded(_Model):
    # Stable machine code (for example MISSING_TRAVEL_TIME).
    code: str
    # Which inputs are missing.
    missing: tuple[str, ...] = ()
    # The question to put to the driver.
    question: str


# The driver's report and the gate record disagree.
class EtaConflict(_Model):
    code: Literal["CONTRADICTORY_ARRIVAL_STATE"]
    detail: str


# What the facility's gate check-in table says about the truck.
class GateEvidence(_Model):
    gate_in_ts: Aware | None = None


# Time window the driver says they can accept.
class DriverConstraints(_Model):
    earliest_acceptable: Aware | None = None
    latest_acceptable: Aware | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        # A window that ends before it starts can never be satisfied: reject it instead of silently finding nothing.
        if (
            self.earliest_acceptable is not None
            and self.latest_acceptable is not None
            and self.latest_acceptable < self.earliest_acceptable
        ):
            raise ValueError("latest_acceptable precedes earliest_acceptable")
        return self


# What unloading this shipment needs from a dock.
class UnloadRequirement(_Model):
    # gt=0: unloading takes some positive number of minutes.
    expected_unload_min: Annotated[int, Field(gt=0)]
    required_dock_type: DockType
    load_weight_kg: Annotated[int, Field(gt=0)]
    temperature_control_required: bool = False


class ConsigneeCommitment(_Model):
    """A known commitment. ``latest_arrival=None`` means known, with no deadline."""

    latest_arrival: Aware | None = None
    # Does missing the deadline cost money?
    penalty_applies: bool = False
    # Is this an exceptional (VIP or regulated) delivery that needs human approval to miss?
    exceptional: bool = False
