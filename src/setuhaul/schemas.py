"""Domain contracts for ETA evidence. All timestamps must be timezone-aware."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from setuhaul.db import IST


def _to_ist(value: datetime) -> datetime:
    return value.astimezone(IST)


Aware = Annotated[AwareDatetime, AfterValidator(_to_ist)]
Confidence = Literal["LOW", "MEDIUM", "HIGH"]
DockType = Literal["ANY", "STANDARD", "REEFER", "HEAVY"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BaselineType(StrEnum):
    PLANNED_ETA = "PLANNED_ETA"
    CURRENT_ETA = "CURRENT_ETA"
    DRIVER_DECLARED = "DRIVER_DECLARED"
    REPAIR_COMPLETION = "REPAIR_COMPLETION"
    GATE_IN = "GATE_IN"


class EtaRecord(_Model):
    """An ETA the system already holds, with the record it came from."""

    record_id: str
    eta: Aware


class DelayKind(StrEnum):
    LATE_BY = "LATE_BY"
    REPAIR = "REPAIR"
    EXPLICIT_ETA = "EXPLICIT_ETA"


class DelayReport(_Model):
    kind: DelayKind
    confidence: Confidence = "MEDIUM"
    delay_minutes: Annotated[int, Field(ge=0)] | None = None
    baseline: BaselineType = BaselineType.PLANNED_ETA
    repair_minutes: Annotated[int, Field(ge=0)] | None = None
    travel_minutes: Annotated[int, Field(ge=0)] | None = None
    repair_started_at: Aware | None = None
    declared_eta: Aware | None = None

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

    @model_validator(mode="after")
    def _kind_fields(self) -> Self:
        needed = {
            DelayKind.LATE_BY: self.delay_minutes,
            DelayKind.REPAIR: self.repair_minutes,
            DelayKind.EXPLICIT_ETA: self.declared_eta,
        }[self.kind]
        if needed is None:
            raise ValueError(f"{self.kind} report is missing its required value")
        return self


class EtaEvidence(_Model):
    eta: Aware
    source: Literal["DRIVER_DECLARED", "GATE_CHECKIN"]
    confidence: Confidence
    baseline_type: BaselineType
    baseline_record_id: str | None = None


class ClarificationNeeded(_Model):
    code: str
    missing: tuple[str, ...] = ()
    question: str


class EtaConflict(_Model):
    code: Literal["CONTRADICTORY_ARRIVAL_STATE"]
    detail: str


class GateEvidence(_Model):
    gate_in_ts: Aware | None = None


class DriverConstraints(_Model):
    earliest_acceptable: Aware | None = None
    latest_acceptable: Aware | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if (
            self.earliest_acceptable is not None
            and self.latest_acceptable is not None
            and self.latest_acceptable < self.earliest_acceptable
        ):
            raise ValueError("latest_acceptable precedes earliest_acceptable")
        return self


class UnloadRequirement(_Model):
    expected_unload_min: Annotated[int, Field(gt=0)]
    required_dock_type: DockType
    load_weight_kg: Annotated[int, Field(gt=0)]
    temperature_control_required: bool = False


class ConsigneeCommitment(_Model):
    """A known commitment. ``latest_arrival=None`` means known, with no deadline."""

    latest_arrival: Aware | None = None
    penalty_applies: bool = False
    exceptional: bool = False
