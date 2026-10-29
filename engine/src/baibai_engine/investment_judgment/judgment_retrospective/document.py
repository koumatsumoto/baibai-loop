"""旧判断への比較メモと、serviceが固定する台帳参照。"""

from __future__ import annotations

from datetime import date, datetime
from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator

from baibai_engine.investment_judgment.company_research.document import ResearchRef
from baibai_engine.investment_judgment.contracts import (
    _CONFIG,
    NonEmptyStr,
    Ticker,
    _date,
    _datetime,
    _tuple,
)
from baibai_engine.investment_judgment.stock_assessment.document import AssessmentRef
from baibai_engine.primitives.json import canonical_json
from baibai_engine.primitives.time import JST

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
AppendHead = Annotated[int, Field(ge=0)]


class ProposalItemRef(BaseModel):
    model_config = _CONFIG
    proposal_id: NonEmptyStr
    draft_sha256: Digest
    ticker: Ticker


type RetrospectiveOrigin = ProposalItemRef | AssessmentRef


class RetrospectiveRef(BaseModel):
    model_config = _CONFIG
    retrospective_id: NonEmptyStr
    payload_sha256: Digest


class RetrospectiveCitation(BaseModel):
    model_config = _CONFIG
    reference: ResearchRef | AssessmentRef
    source_ids: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    fact_ids: tuple[NonEmptyStr, ...] = ()

    @field_validator("source_ids", "fact_ids", mode="before")
    @classmethod
    def sequences(cls, value: object) -> object:
        return _tuple(value)


class RetrospectiveComparison(BaseModel):
    model_config = _CONFIG
    original_claim: NonEmptyStr
    observed: NonEmptyStr
    result: Literal["supported", "refuted", "unresolved"]
    reason: NonEmptyStr
    citations: tuple[RetrospectiveCitation, ...] = ()

    @field_validator("citations", mode="before")
    @classmethod
    def sequences(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def evidence(self) -> Self:
        if self.result != "unresolved" and not self.citations:
            raise ValueError("supported/refuted comparison requires a citation")
        return self


class RetrospectiveLesson(BaseModel):
    model_config = _CONFIG
    next_time: NonEmptyStr
    rationale: NonEmptyStr


class RetrospectiveDocument(BaseModel):
    model_config = _CONFIG
    kind: Literal["judgment_retrospective"] = "judgment_retrospective"
    schema_version: Literal[1] = 1
    ticker: Ticker
    as_of: date
    milestone: NonEmptyStr
    origin: RetrospectiveOrigin
    comparisons: tuple[RetrospectiveComparison, ...] = ()
    lesson: RetrospectiveLesson | None = None
    corrects_ref: RetrospectiveRef | None = None

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

    @field_validator("comparisons", mode="before")
    @classmethod
    def sequences(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def content(self) -> Self:
        if not self.comparisons and self.lesson is None:
            raise ValueError("retrospective requires a comparison or lesson")
        if isinstance(self.origin, ProposalItemRef) and self.origin.ticker != self.ticker:
            raise ValueError("origin ticker differs from retrospective")
        if self.corrects_ref is not None and self.lesson is None:
            raise ValueError("correction requires its reason in lesson")
        return self


class LedgerSnapshot(BaseModel):
    """作者の選択ではなく、固定headとoriginからserviceが導出した集合。"""

    model_config = _CONFIG
    append_head: AppendHead
    event_ids: tuple[NonEmptyStr, ...]

    @field_validator("event_ids", mode="before")
    @classmethod
    def sequences(cls, value: object) -> object:
        return _tuple(value)


class RetrospectiveDraft(BaseModel):
    model_config = _CONFIG
    retrospective_id: NonEmptyStr
    document: RetrospectiveDocument
    # None means not read/unavailable, not an instruction to use the current head.
    ledger_append_head: AppendHead | None = None


class RetrospectivePayload(BaseModel):
    model_config = _CONFIG
    document: RetrospectiveDocument
    ledger: LedgerSnapshot | None


def retrospective_sha256(payload: RetrospectivePayload) -> str:
    return sha256(canonical_json(payload.model_dump(mode="json")).encode()).hexdigest()


class RetrospectiveRecord(RetrospectivePayload):
    reference: RetrospectiveRef
    recorded_at: AwareDatetime

    @field_validator("recorded_at", mode="before")
    @classmethod
    def parse_time(cls, value: object) -> datetime:
        return _datetime(value)

    @model_validator(mode="after")
    def identity(self) -> Self:
        payload = RetrospectivePayload(document=self.document, ledger=self.ledger)
        if retrospective_sha256(payload) != self.reference.payload_sha256:
            raise ValueError("Retrospective payload digest mismatch")
        if self.document.as_of > self.recorded_at.astimezone(JST).date():
            raise ValueError("Retrospective as_of is after recorded_at")
        if (
            self.document.corrects_ref is not None
            and self.document.corrects_ref.retrospective_id == self.reference.retrospective_id
        ):
            raise ValueError("Retrospective cannot correct itself")
        return self
