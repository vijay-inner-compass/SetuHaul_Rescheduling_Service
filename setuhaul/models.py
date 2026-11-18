"""Pydantic argument schemas attached to TOOLS (S6: args_schema), never to the model.
Shape only; facts (is the slot free?) are the database's job, intent is the driver's."""
from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field, model_validator

class NoArgs(BaseModel): pass

class ReportDelay(BaseModel):
    shipment_id: str = Field(min_length=3, max_length=20)
    delay_minutes: Optional[int] = Field(default=None, ge=1, le=24*60, strict=True)
    new_eta: Optional[datetime] = None              # ISO local time if driver gave a clock time
    source: Literal["driver_declared", "driver_estimate_with_repair"] = "driver_declared"
    @model_validator(mode="after")
    def one_of(self):
        if (self.delay_minutes is None) == (self.new_eta is None):
            raise ValueError("give exactly one of delay_minutes or new_eta")
        return self

class FindOptions(BaseModel):
    shipment_id: str = Field(min_length=3, max_length=20)
    not_before: Optional[datetime] = None           # "anything after 7"
    limit: int = Field(default=3, ge=1, le=5, strict=True)

class HoldOption(BaseModel):
    shipment_id: str = Field(min_length=3, max_length=20)
    slot_id: str = Field(min_length=2, max_length=30)
    idempotency_key: str = Field(min_length=8, max_length=64)   # S2: one key per driver action

class ConfirmBooking(BaseModel):
    hold_id: int = Field(ge=1, strict=True)

class ReleaseHold(BaseModel):
    hold_id: int = Field(ge=1, strict=True)

class Escalate(BaseModel):
    shipment_id: str = Field(min_length=3, max_length=20)
    reason: str = Field(min_length=3, max_length=300)
