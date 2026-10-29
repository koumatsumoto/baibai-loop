"""部分調査の自己完結snapshot。価格評価・Reviewの完成条件は持たない。"""

from __future__ import annotations

from datetime import date, datetime
from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator

from baibai_engine.investment_judgment.company_research.contracts import (
    HypothesisCheck,
    ObservedFact,
    Source,
)
from baibai_engine.investment_judgment.contracts import (
    _CONFIG,
    NonEmptyStr,
    Ticker,
    _date,
    _datetime,
    _tuple,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.primitives.time import JST


class ResearchRef(BaseModel):
    model_config = _CONFIG
    research_id: NonEmptyStr
    payload_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class EvidenceNote(BaseModel):
    model_config = _CONFIG
    text: NonEmptyStr
    source_ids: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]

    @field_validator("source_ids", mode="before")
    @classmethod
    def sequences(cls, value: object) -> object:
        return _tuple(value)


class ResearchDocument(BaseModel):
    model_config = _CONFIG
    kind: Literal["company_research"] = "company_research"
    schema_version: Literal[1] = 1
    ticker: Ticker
    as_of: date
    investigated_scope: NonEmptyStr
    sources: tuple[Source, ...] = ()
    facts: tuple[ObservedFact, ...] = ()
    business_understanding: EvidenceNote | None = None
    value_capture_hypothesis: EvidenceNote | None = None
    hypotheses: tuple[HypothesisCheck, ...] = ()
    unresolved_questions: tuple[NonEmptyStr, ...] = ()

    @field_validator("schema_version", mode="before")
    @classmethod
    def version(cls, value: object) -> object:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("schema_version must be an integer")
        return value

    @field_validator("as_of", mode="before")
    @classmethod
    def parse_date(cls, value: object) -> date:
        return _date(value)

    @field_validator("sources", "facts", "hypotheses", "unresolved_questions", mode="before")
    @classmethod
    def sequences(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def evidence(self) -> Self:
        if not any(
            (
                self.facts,
                self.business_understanding,
                self.value_capture_hypothesis,
                self.hypotheses,
                self.unresolved_questions,
            )
        ):
            raise ValueError("research requires a fact, understanding, hypothesis or question")
        sources = {source.source_id: source for source in self.sources}
        if len(sources) != len(self.sources):
            raise ValueError("source_id must be unique")
        if len({fact.fact_id for fact in self.facts}) != len(self.facts):
            raise ValueError("fact_id must be unique")
        for source in self.sources:
            if source.ticker != self.ticker:
                raise ValueError(f"source {source.source_id} ticker mismatch")
            if (
                source.as_of > self.as_of
                or source.retrieved_at.astimezone(JST).date() < source.as_of
            ):
                raise ValueError(f"source {source.source_id} has invalid time")
        for fact in self.facts:
            if not fact.source_ids or not set(fact.source_ids) <= sources.keys():
                raise ValueError("fact has unknown or missing source reference")
            if fact.as_of > self.as_of or any(
                sources[ref].as_of < fact.as_of for ref in fact.source_ids
            ):
                raise ValueError(f"fact {fact.fact_id} postdates its source or snapshot")
            if (
                fact.observed_at is not None
                and fact.observed_at.astimezone(JST).date() != fact.as_of
            ):
                raise ValueError(f"fact {fact.fact_id} observation date mismatch")
        for item in (self.business_understanding, self.value_capture_hypothesis, *self.hypotheses):
            if item is not None and not set(item.source_ids) <= sources.keys():
                raise ValueError("understanding or hypothesis references unknown source")
        return self


def research_sha256(document: ResearchDocument) -> str:
    return sha256(canonical_json(document.model_dump(mode="json")).encode()).hexdigest()


class ResearchRecord(BaseModel):
    model_config = _CONFIG
    reference: ResearchRef
    recorded_at: AwareDatetime
    prior_research_ref: ResearchRef | None
    document: ResearchDocument

    @field_validator("recorded_at", mode="before")
    @classmethod
    def parse_time(cls, value: object) -> datetime:
        return _datetime(value)

    @model_validator(mode="after")
    def identity(self) -> Self:
        if research_sha256(self.document) != self.reference.payload_sha256:
            raise ValueError("Research payload digest mismatch")
        if (
            self.prior_research_ref is not None
            and self.prior_research_ref.research_id == self.reference.research_id
        ):
            raise ValueError("Research cannot follow itself")
        if self.document.as_of > self.recorded_at.astimezone(JST).date():
            raise ValueError("Research as_of is after recorded_at")
        if any(source.retrieved_at > self.recorded_at for source in self.document.sources):
            raise ValueError("source retrieval is after recorded_at")
        if any(
            fact.observed_at is not None and fact.observed_at > self.recorded_at
            for fact in self.document.facts
        ):
            raise ValueError("fact observation is after recorded_at")
        return self


class ResearchDraft(BaseModel):
    """保存入力。記録時刻は作者でなくservice clockが与える。"""

    model_config = _CONFIG
    research_id: NonEmptyStr
    prior_research_ref: ResearchRef | None = None
    document: ResearchDocument


def scaffold_research(
    *, research_id: str, ticker: str, as_of: date, prior: ResearchRecord | None = None
) -> dict[str, object]:
    """元日付・反証・問いをコピーする。空の調査範囲は保存前に今回の内容へ直す。"""
    if prior is not None and (prior.document.ticker != ticker or as_of < prior.document.as_of):
        raise ValueError("scaffold ticker/as_of differs from prior Research")
    document = (
        prior.document.model_dump(mode="json")
        if prior is not None
        else {
            "kind": "company_research",
            "schema_version": 1,
            "ticker": ticker,
            "sources": [],
            "facts": [],
            "business_understanding": None,
            "value_capture_hypothesis": None,
            "hypotheses": [],
            "unresolved_questions": [],
        }
    )
    document.update(as_of=as_of.isoformat(), investigated_scope="")
    return {
        "research_id": research_id,
        "prior_research_ref": None if prior is None else prior.reference.model_dump(mode="json"),
        "document": document,
    }
