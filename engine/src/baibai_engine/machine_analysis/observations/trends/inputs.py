"""seal内で読み終えた抽出集合。選択と比較元を明示し、可変latestへ戻らない。"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from baibai_engine.machine_analysis.observations.trends.contracts import (
    SelectedArtifact,
    TrendInputManifest,
    Value,
)


class IndustryScope(Value):
    scope_id: str
    label: str
    taxonomy: str
    geography: str


class TrendCoverage(Value):
    state: Literal["available", "not_collected", "extraction_pending", "partial", "stale"]
    reasons: tuple[str, ...] = ()
    # The reader fixes a finite window and records whether more retained evidence
    # exists. This descriptor is for the same release, never a mutable latest URL.
    omitted_artifacts: int = Field(default=0, ge=0)
    continuation: str | None = None
    source_states: tuple[str, ...] = ()
    latest_receipt_at: AwareDatetime | None = None


class FixedTrendInputs(Value):
    manifest: TrendInputManifest
    artifacts: tuple[SelectedArtifact, ...] = Field(max_length=16)
    coverage: TrendCoverage

    @model_validator(mode="after")
    def exact_selection(self) -> Self:
        selected = tuple(item.current.ref for item in self.artifacts)
        baselines = tuple(
            sorted(
                {item.baseline.ref for item in self.artifacts if item.baseline is not None},
                key=lambda ref: ref.artifact_id,
            )
        )
        if selected != self.manifest.selected or baselines != self.manifest.baselines:
            raise ValueError("selected artifacts differ from the fixed manifest")
        if len(set(selected)) != len(selected):
            raise ValueError("duplicate selected artifact")
        if self.manifest.window_start > self.manifest.window_end:
            raise ValueError("invalid observation window")
        for item in self.artifacts:
            for artifact in (item.current, item.baseline):
                if artifact is not None and (
                    artifact.evidence.received_at > self.manifest.cutoff
                    or artifact.created_at > self.manifest.cutoff
                ):
                    raise ValueError("artifact is after the fixed cutoff")
        if self.coverage.omitted_artifacts and not self.coverage.continuation:
            raise ValueError("omitted artifacts need a fixed-release continuation")
        return self


class IndustryInput(Value):
    definition: IndustryScope
    inputs: FixedTrendInputs
