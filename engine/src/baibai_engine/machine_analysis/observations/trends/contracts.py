"""帰属付きの原文主張。保存・採用・モデル判断を含まない共通値型。"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from baibai_engine.primitives.json import canonical_json

type Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
type Name = Annotated[str, Field(min_length=1)]
type ExtractionStatus = Literal["extracted", "empty", "unsupported", "failed", "pending"]


class Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class TextEvidence(Value):
    article_id: Name
    receipt_id: Name
    source: Name
    source_url: Name
    raw_sha256: Digest
    receipt_sha256: Digest | None = None
    body_receipt_id: str | None = None
    observation_id: str | None = None
    observation_sha256: Digest | None = None
    text_sha256: Digest
    text_revision: Name
    coordinate_system: Literal["unicode_codepoint_half_open"] = "unicode_codepoint_half_open"
    published_at: AwareDatetime | None
    published_date: date | None = None
    published_raw: str | None = None
    published_precision: Literal["unknown", "date", "minute", "second"] = "unknown"
    received_at: AwareDatetime
    content_scope: Literal["headline", "summary", "full_text", "partial_text"]


class DocumentText(Value):
    evidence: TextEvidence
    text: str

    @model_validator(mode="after")
    def verify_projection(self) -> Self:
        if hashlib.sha256(self.text.encode()).hexdigest() != self.evidence.text_sha256:
            raise ValueError("text projection digest differs")
        return self


def project_text(text: str, **evidence: object) -> DocumentText:
    """取得済み本文からparser用座標を固定。PDF/HTMLのnative解釈はsource owner。"""
    projected = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    return DocumentText(
        text=projected,
        evidence=TextEvidence.model_validate(
            {
                **evidence,
                "text_sha256": hashlib.sha256(projected.encode()).hexdigest(),
                "text_revision": "nfkc-no-whitespace-v1",
            }
        ),
    )


class Span(Value):
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def positive_length(self) -> Self:
        if self.end <= self.start:
            raise ValueError("empty evidence span")
        return self


class ClaimScope(Value):
    scope_id: Name
    taxonomy_revision: Name
    label: Name
    geography: str | None = None
    population: str | None = None
    population_id: str | None = None
    population_size: Annotated[int, Field(gt=0)] | None = None
    period: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    period_basis: str | None = None
    target: str | None = None


class Quantity(Value):
    value: Decimal
    unit: Name
    # Resolution in this unit, not claimed measurement accuracy.
    resolution: Decimal | None = None
    approximate: bool = False
    basis: str | None = None

    @model_validator(mode="after")
    def finite_value(self) -> Self:
        if not self.value.is_finite() or (
            self.resolution is not None
            and (not self.resolution.is_finite() or self.resolution <= 0)
        ):
            raise ValueError("quantity and resolution must be finite; resolution must be positive")
        return self


class Claim(Value):
    claim_id: Name
    domain: Literal["industry", "social"]
    kind: Literal["reported_fact", "announced_plan", "forecast_claim", "causal_claim"]
    predicate: Name
    scope: ClaimScope
    attribution: Name
    spans: tuple[Span, ...] = Field(min_length=1)
    quantity: Quantity | None = None
    status: Literal[
        "reported",
        "considered",
        "announced",
        "decided",
        "scheduled",
        "implementation_reported",
        "changed",
        "lifted",
        "rejected",
    ] = "reported"
    occurred_on: date | None = None
    scheduled_for: date | None = None
    negated: bool = False
    conditions: tuple[str, ...] = ()
    uncertainty: tuple[str, ...] = ()
    # Only an explicit official identifier/link; never title similarity.
    explicit_event_id: str | None = None


class ArtifactRef(Value):
    artifact_id: Name
    sha256: Digest


class ExtractionArtifact(Value):
    evidence: TextEvidence
    method: Literal["parser", "assistant"]
    method_revision: Name
    family: Name
    created_at: AwareDatetime
    status: ExtractionStatus
    claims: tuple[Claim, ...] = ()
    limitations: tuple[str, ...] = ()
    # An assistant producer must identify its frozen model/prompt/config.
    assistant_config: str | None = None
    # In-memory view of the canonical storage envelope. Never embedded in its
    # own payload or substituted for a different receipt after a repeated GET.
    storage_ref: ArtifactRef | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def coherent_artifact(self) -> Self:
        if self.status in ("failed", "pending", "unsupported") and self.claims:
            raise ValueError("unsuccessful extraction cannot carry current claims")
        if len({claim.claim_id for claim in self.claims}) != len(self.claims):
            raise ValueError("duplicate claim identity")
        if self.method == "assistant" and not self.assistant_config:
            raise ValueError("assistant extraction needs a frozen configuration identifier")
        return self

    @property
    def ref(self) -> ArtifactRef:
        if self.storage_ref is not None:
            return self.storage_ref
        sha256 = digest(self.model_dump(mode="json"))
        return ArtifactRef(artifact_id=f"extraction:{sha256}", sha256=sha256)

    def verify_text(self, document: DocumentText) -> None:
        if self.evidence != document.evidence:
            raise ValueError("artifact text identity differs")
        if any(span.end > len(document.text) for c in self.claims for span in c.spans):
            raise ValueError("claim span is outside the text projection")


class SelectedArtifact(Value):
    """採用は明示的。失敗時も新版refを選び、旧値を現行へ再包装しない。"""

    current: ExtractionArtifact
    baseline: ExtractionArtifact | None = None


class TrendInputManifest(Value):
    release_id: Name
    release_sha256: Digest
    cutoff: AwareDatetime
    selection_revision: Name
    scope_revision: Name
    window_start: date
    window_end: date
    selected: tuple[ArtifactRef, ...]
    baselines: tuple[ArtifactRef, ...]

    @property
    def sha256(self) -> str:
        return digest(self.model_dump(mode="json"))
