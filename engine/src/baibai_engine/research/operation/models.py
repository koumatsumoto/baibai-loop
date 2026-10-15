"""Contracts for one trigger occurrence and its current operation payload."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

SessionKind = Literal[
    "capital-allocation",
    "position-review",
]
OperationStatus = Literal["active", "completed"]


class OperationPayload(BaseModel):
    """The replaceable active checkpoint or immutable final operation result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint: str = Field(min_length=1)
    artifacts: tuple[dict[str, JsonValue], ...] = ()
    canonical_refs: tuple[str, ...] = ()
    result: str | None = Field(default=None, min_length=1)
    next: str | None = Field(default=None, min_length=1)


class OperationSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str = Field(pattern=r"^research-op-[0-9a-f]{32}$")
    session_kind: SessionKind
    status: OperationStatus
    as_of: date
    ticker: str | None = Field(default=None, pattern=r"^[0-9A-Z]{4}$")
    started_at: datetime
    completed_at: datetime | None = None
    payload: OperationPayload

    @model_validator(mode="after")
    def validate_completion_time(self) -> OperationSession:
        if self.started_at.utcoffset() is None:
            raise ValueError("started_at must include a UTC offset")
        if (self.status == "completed") != (self.completed_at is not None):
            raise ValueError("completed_at must be present exactly when status is completed")
        if self.completed_at is not None and self.completed_at.utcoffset() is None:
            raise ValueError("completed_at must include a UTC offset")
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        return self

    def public(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], self.model_dump(mode="json"))


SESSION_KINDS: tuple[SessionKind, ...] = (
    "capital-allocation",
    "position-review",
)


__all__ = [
    "SESSION_KINDS",
    "OperationPayload",
    "OperationSession",
    "OperationStatus",
    "SessionKind",
]
