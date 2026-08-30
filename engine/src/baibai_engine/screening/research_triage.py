"""Canonical judgment of whether each Review Set entry merits research time."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.write import connect_rw, initialize_database

RESEARCH_TRIAGE_SCHEMA_VERSION = 1
RESEARCH_TRIAGE_CONTRACT_ID = "research-triage-v1"


class ResearchTriageMachineSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    review_position: int = Field(ge=1)
    nominations: tuple[Mapping[str, object], ...] | None
    support_count: int | None = Field(default=None, ge=1, le=4)
    expected_return: Mapping[str, object] | None = None
    fair_value: Mapping[str, object] | None = None
    data_quality: Mapping[str, object] | None = None


class ResearchTriageEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ticker: str = Field(pattern=r"^[0-9A-Z]{4}$")
    decision: Literal["research", "skip"]
    priority: int | None = Field(default=None, ge=1)
    rationale: str = Field(min_length=1)
    research_question: str | None = None
    key_risk: str | None = None
    machine_snapshot: ResearchTriageMachineSnapshot | None = None

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

    schema_version: Literal[1]
    kind: Literal["research_triage"]
    research_triage_id: str = Field(min_length=1)
    review_set_id: str = Field(min_length=1)
    run_revision_id: str = Field(min_length=1)
    as_of: date
    published_at: datetime
    macro_context_id: str | None = None
    review_basis_research_triage_id: str | None
    triage_contract_id: Literal["research-triage-v1"]
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


class ResearchTriageConflictError(ValueError):
    pass


class ResearchTriageService:
    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path

    def publish(
        self,
        triage: ResearchTriage,
        *,
        review_set: Mapping[str, object],
    ) -> ResearchTriage:
        if triage.review_set_id != review_set.get("review_set_id"):
            raise ResearchTriageConflictError("research triage review-set binding differs")
        if triage.run_revision_id != review_set.get("run_revision_id"):
            raise ResearchTriageConflictError("research triage run binding differs")
        if triage.as_of.isoformat() != review_set.get("as_of"):
            raise ResearchTriageConflictError("research triage as-of differs")
        review_entries = review_set.get("entries")
        if not isinstance(review_entries, list):
            raise ResearchTriageConflictError("source Review Set entries are unavailable")
        by_ticker = {
            str(entry.get("ticker")): entry
            for entry in review_entries
            if isinstance(entry, Mapping) and isinstance(entry.get("ticker"), str)
        }
        if {entry.ticker for entry in triage.entries} != set(by_ticker):
            raise ResearchTriageConflictError("triage entries must equal the Review Set")
        review_basis = review_set.get("review_basis")
        expected_basis = (
            review_basis.get("judged_through_research_triage_id")
            if isinstance(review_basis, Mapping)
            else None
        )
        if triage.review_basis_research_triage_id != expected_basis:
            raise ResearchTriageConflictError("research triage Review Basis differs")
        entries = []
        for entry in triage.entries:
            source = by_ticker[entry.ticker]
            analysis = source.get("analysis")
            analysis_map = analysis if isinstance(analysis, Mapping) else {}
            entries.append(
                entry.model_copy(
                    update={
                        "machine_snapshot": ResearchTriageMachineSnapshot(
                            review_position=int(source["review_position"]),
                            nominations=tuple(source.get("nominations", ())),
                            support_count=int(source["support_count"]),
                            expected_return=_mapping_or_none(analysis_map.get("expected_return")),
                            fair_value=None,
                            data_quality=_mapping_or_none(analysis_map.get("data_quality")),
                        )
                    }
                )
            )
        triage = triage.model_copy(update={"entries": tuple(entries)})
        initialize_database(self._db_path)
        payload = canonical_json(triage.model_dump(mode="json"))
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT payload FROM research_triage WHERE research_triage_id = ?",
                    (triage.research_triage_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) == payload:
                        connection.rollback()
                        return triage
                    raise ResearchTriageConflictError("research triage identity already differs")
                latest = connection.execute(
                    "SELECT research_triage_id FROM research_triage "
                    "ORDER BY as_of DESC, published_at DESC, research_triage_id DESC LIMIT 1"
                ).fetchone()
                latest_id = str(latest[0]) if latest is not None else None
                if triage.review_basis_research_triage_id != latest_id:
                    raise ResearchTriageConflictError("research triage Review Basis is stale")
                connection.execute(
                    "INSERT INTO research_triage "
                    "(research_triage_id, review_set_id, run_revision_id, "
                    "as_of, published_at, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        triage.research_triage_id,
                        triage.review_set_id,
                        triage.run_revision_id,
                        triage.as_of.isoformat(),
                        triage.published_at.isoformat(),
                        payload,
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return triage


def _mapping_or_none(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


__all__ = [
    "RESEARCH_TRIAGE_CONTRACT_ID",
    "RESEARCH_TRIAGE_SCHEMA_VERSION",
    "ResearchTriage",
    "ResearchTriageConflictError",
    "ResearchTriageEntry",
    "ResearchTriageMachineSnapshot",
    "ResearchTriageService",
]
