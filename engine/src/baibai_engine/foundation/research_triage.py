"""Stop T1 admission drift with one current Triage contract for writer and read gates."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .candidate_discovery import Nomination, ReviewSetAnalysis, ReviewSetMethod

RESEARCH_TRIAGE_SCHEMA_VERSION = 2
RESEARCH_TRIAGE_CONTRACT_ID = "research-triage-v2"


class ResearchTriageCandidateSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str
    sector_33: str
    review_position: int = Field(ge=1)
    nominations: tuple[Nomination, ...]
    analysis: ReviewSetAnalysis

    @field_validator("nominations", mode="before")
    @classmethod
    def _tuple_nominations(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class ResearchTriageEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ticker: str = Field(pattern=r"^[0-9A-Z]{4}$")
    decision: Literal["research", "skip"]
    priority: int | None = Field(default=None, ge=1)
    rationale: str = Field(min_length=1)
    research_question: str | None = None
    key_risk: str | None = None
    candidate_snapshot: ResearchTriageCandidateSnapshot

    @field_validator("rationale", "research_question", "key_risk")
    @classmethod
    def _reject_scaffold_placeholder(cls, value: str | None) -> str | None:
        if value is not None and value.strip().upper().startswith("TODO"):
            raise ValueError("replace scaffold TODO text before publication")
        return value

    @model_validator(mode="after")
    def _decision_shape(self) -> Self:
        if self.decision == "research":
            if self.priority is None or not self.research_question or not self.key_risk:
                raise ValueError("research requires priority, research_question, and key_risk")
        elif any(
            value is not None for value in (self.priority, self.research_question, self.key_risk)
        ):
            raise ValueError("skip forbids priority, research_question, and key_risk")
        return self


class ResearchTriage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[2]
    kind: Literal["research_triage"]
    research_triage_id: str = Field(min_length=1)
    review_set_id: str = Field(min_length=1)
    run_revision_id: str = Field(min_length=1)
    as_of: date
    published_at: datetime
    macro_context_id: str | None = None
    expected_prior_research_triage_id: str | None
    screening_rules_hash: str = Field(min_length=1)
    candidate_discovery_method: ReviewSetMethod
    triage_contract_id: Literal["research-triage-v2"]
    entries: tuple[ResearchTriageEntry, ...] = Field(min_length=1)

    @field_validator("as_of", mode="before")
    @classmethod
    def _parse_as_of(cls, value: object) -> object:
        return date.fromisoformat(value) if isinstance(value, str) else value

    @field_validator("published_at", mode="before")
    @classmethod
    def _parse_published_at(cls, value: object) -> object:
        return datetime.fromisoformat(value) if isinstance(value, str) else value

    @field_validator("entries", mode="before")
    @classmethod
    def _tuple_entries(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _publication_shape(self) -> Self:
        if self.published_at.tzinfo is None:
            raise ValueError("published_at must include a timezone")
        tickers = [entry.ticker for entry in self.entries]
        if len(tickers) != len(set(tickers)):
            raise ValueError("research triage tickers must be unique")
        priorities = sorted(entry.priority for entry in self.entries if entry.priority is not None)
        if priorities != list(range(1, len(priorities) + 1)):
            raise ValueError("research priorities must be contiguous from 1")
        return self

    def researchable_tickers(self) -> tuple[str, ...]:
        return tuple(
            entry.ticker
            for entry in sorted(self.entries, key=lambda item: item.priority or 10**9)
            if entry.decision == "research"
        )


__all__ = [
    "RESEARCH_TRIAGE_CONTRACT_ID",
    "RESEARCH_TRIAGE_SCHEMA_VERSION",
    "ResearchTriage",
    "ResearchTriageCandidateSnapshot",
    "ResearchTriageEntry",
]
