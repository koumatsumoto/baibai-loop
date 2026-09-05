"""Reject malformed model judgments before the engine's canonical boundary."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from baibai_engine.batch_api import DailyTriageDecision, ReviewSetEntrySnapshot


class MacroProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["missing", "current", "stale"]
    as_of: str | None = None
    age_days: int | None = Field(default=None, ge=0)
    summary: str | None = None
    synthesis: dict[str, object] | None = None
    connection: dict[str, object] | None = None


class TriageCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ticker: str = Field(pattern=r"^[0-9A-Z]{4}$")
    snapshot: ReviewSetEntrySnapshot


class ModelInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    task: Literal["research-triage"]
    instruction: str
    policy: tuple[str, ...]
    as_of: str
    macro_context: MacroProjection
    candidates: tuple[TriageCandidate, ...] = Field(min_length=1, max_length=80)

    @field_validator("policy", "candidates", mode="before")
    @classmethod
    def _tuples(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class ModelOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    decisions: tuple[DailyTriageDecision, ...] = Field(min_length=1, max_length=80)

    @field_validator("decisions", mode="before")
    @classmethod
    def _tuple_decisions(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _unique_tickers(self) -> Self:
        tickers = [decision.ticker for decision in self.decisions]
        if len(tickers) != len(set(tickers)):
            raise ValueError("AI decision tickers must be unique")
        priorities = sorted(
            decision.priority for decision in self.decisions if decision.priority is not None
        )
        if priorities != list(range(1, len(priorities) + 1)):
            raise ValueError("research priorities must be contiguous from 1")
        return self


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model_requests: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    duration_seconds: float = Field(ge=0)
    tool_calls: int = Field(ge=0)


__all__ = [
    "MacroProjection",
    "ModelInput",
    "ModelOutput",
    "ModelUsage",
    "TriageCandidate",
]
