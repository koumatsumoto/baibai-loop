"""明示作成する小さな市場環境判断と、保存履歴の読取envelope。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from baibai_engine.primitives.time import JST

ASSESSMENT_SCHEMA_VERSION = 6
ASSESSMENT_STALE_DAYS = 45


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AssessmentInput(_Model):
    input_id: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    layer: Literal["L1", "L2", "primary_source"]
    observation_as_of: date | None = None
    available_at: datetime
    status: Literal["ok", "missing", "failed"] = "ok"
    series_ids: tuple[str, ...] = ()
    method_revision: str | None = None

    @field_validator("available_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        return require_aware(value)


class AssessmentClaim(_Model):
    summary: str = Field(min_length=1)
    source_ids: tuple[str, ...] = ()

    @field_validator("summary")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("claim summary must be non-blank")
        return value


class MarketContextAssessment(_Model):
    schema_version: Literal[6] = 6
    kind: Literal["market-context-assessment"] = "market-context-assessment"
    # Only new publications use this namespace; historical identities remain opaque.
    context_id: str
    as_of: date
    known_at: datetime
    published_at: datetime
    summary: str = Field(min_length=1)
    inputs: tuple[AssessmentInput, ...]
    changes: tuple[AssessmentClaim, ...] = ()
    counter_evidence: tuple[AssessmentClaim, ...] = ()
    uncertainties: tuple[AssessmentClaim, ...] = ()
    implications: tuple[AssessmentClaim, ...] = ()

    @field_validator("known_at", "published_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if not re.fullmatch(
            r"market-context-assessment-\d{4}-\d{2}-\d{2}-[a-z0-9-]+", self.context_id
        ):
            raise ValueError("invalid context_id")
        if not self.context_id.startswith(f"market-context-assessment-{self.as_of.isoformat()}-"):
            raise ValueError("context_id date differs from as_of")
        if not self.summary.strip():
            raise ValueError("assessment summary must be non-blank")
        if self.known_at.astimezone(UTC) > self.published_at.astimezone(UTC):
            raise ValueError("input known_at must not follow publication")
        if self.published_at.astimezone(JST).date() < self.as_of:
            raise ValueError("publication must not predate the assessment day")
        ids = [item.input_id for item in self.inputs]
        if len(ids) != len(set(ids)):
            raise ValueError("input ids must be unique")
        if any(
            item.available_at.astimezone(UTC) > self.known_at.astimezone(UTC)
            for item in self.inputs
        ):
            raise ValueError("input was unavailable at known_at")
        for claim in (
            *self.changes,
            *self.counter_evidence,
            *self.uncertainties,
            *self.implications,
        ):
            if set(claim.source_ids) - set(ids):
                raise ValueError("claim cites an unknown input")
        return self

    def payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value


@dataclass(frozen=True, slots=True)
class StoredAssessment:
    """Publication metadata with the original body, without a historical writer validator."""

    context_id: str
    schema_version: int
    as_of: date
    published_at: datetime
    summary: str
    document: dict[str, Any]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> StoredAssessment:
        record = cls(
            context_id=str(payload["context_id"]),
            schema_version=int(payload["schema_version"]),
            as_of=date.fromisoformat(str(payload["as_of"])),
            published_at=require_aware(datetime.fromisoformat(str(payload["published_at"]))),
            summary=str(payload["summary"]),
            document=payload,
        )
        if record.schema_version == ASSESSMENT_SCHEMA_VERSION:
            MarketContextAssessment.model_validate(payload)
        return record
