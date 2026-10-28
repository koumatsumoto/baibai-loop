"""モデル生成の共通外枠。各モデルの意味と候補政策をここへ集約しない。"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

type Identifier = Annotated[str, Field(min_length=1)]
type Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class InputSnapshot(BaseModel):
    """使用した入力の同一性。これだけで過去の受領時点への巻戻しを保証しない。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["lake_release", "sqlite_snapshot"]
    reference: Identifier
    sha256: Digest


class GeneratedModel(BaseModel):
    """payloadはownerの結果形式を保持し、異なるモデルの値を平坦化しない。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    subject: Identifier
    calculation_revision: Identifier
    payload: dict[str, JsonValue]


class GenerationSubject(BaseModel):
    """一世代で扱う対象と予定モデル。候補政策・重み・実行DAGは含めない。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    scope: Literal["stock", "sector", "market"]
    model_ids: tuple[Identifier, ...]
    related_subjects: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def unique_members(self) -> Self:
        if len(set(self.model_ids)) != len(self.model_ids):
            raise ValueError("planned model IDs must be unique")
        if len(set(self.related_subjects)) != len(self.related_subjects):
            raise ValueError("related subjects must be unique")
        return self


class GenerationMetadata(BaseModel):
    """計算対象日・実行時刻と固定入力。モデルを読み込まない一覧にも使う。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1, 2] = 1
    generation_id: Identifier
    as_of: date
    evaluation_time: AwareDatetime
    inputs: dict[Identifier, InputSnapshot] = Field(min_length=1)

    # V1 must serialize byte-for-byte as before, including when explicitly parsed
    # again. A missing plan is not today's catalog applied to historical output.
    subject_plan: dict[Identifier, GenerationSubject] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def versioned_scope(self) -> Self:
        if (self.schema_version == 1) != (self.subject_plan is None):
            raise ValueError("generation V1 has no subject plan; V2 requires its fixed plan")
        return self


def validate_full_plan(metadata: GenerationMetadata) -> None:
    plan = metadata.subject_plan
    if plan is None:
        return
    for subject, definition in plan.items():
        for related in definition.related_subjects:
            if related not in plan or plan[related].scope == "stock" or related == subject:
                raise ValueError("related subject must name another planned shared context")


def research_subjects(metadata: GenerationMetadata, targets: Sequence[str]) -> tuple[str, ...]:
    """対象株とその共有文脈を解決する。別企業を候補へ増やさない。"""
    plan = metadata.subject_plan
    if plan is None:
        return tuple(sorted({"market", *targets}))
    selected = set(targets)
    if any(subject not in plan or plan[subject].scope != "stock" for subject in selected):
        raise ValueError("research target is outside the generation stock scope")
    selected.update(subject for subject, definition in plan.items() if definition.scope == "market")
    pending = list(selected)
    while pending:
        subject = pending.pop()
        for related in plan[subject].related_subjects:
            if related not in plan or plan[related].scope == "stock":
                raise ValueError("fixed model input is missing its shared subject context")
            if related not in selected:
                selected.add(related)
                pending.append(related)
    return tuple(sorted(selected))


def validate_model_scope(metadata: GenerationMetadata, model: GeneratedModel) -> None:
    if metadata.subject_plan is None:
        return
    definition = metadata.subject_plan.get(model.subject)
    if definition is None or model.model_id not in definition.model_ids:
        raise ValueError("generated model is outside the generation subject plan")


class _ModelCollection(GenerationMetadata):
    """固定入力に属する結果。完全生成と対象別sliceを区別する。"""

    models: tuple[GeneratedModel, ...]

    @field_validator("models")
    @classmethod
    def canonical_model_order(
        cls, models: tuple[GeneratedModel, ...]
    ) -> tuple[GeneratedModel, ...]:
        return tuple(sorted(models, key=lambda model: (model.model_id, model.subject)))

    @model_validator(mode="after")
    def unique_model_subjects(self) -> Self:
        identities = {(model.model_id, model.subject) for model in self.models}
        if len(identities) != len(self.models):
            raise ValueError("a generation must have one result per model and subject")
        for model in self.models:
            validate_model_scope(self, model)
        return self

    def result(self, model_id: str, subject: str) -> GeneratedModel | None:
        """明示されたモデルだけ参照する。未生成を0点や他モデルで代用しない。"""
        return next(
            (item for item in self.models if (item.model_id, item.subject) == (model_id, subject)),
            None,
        )


class ModelGeneration(_ModelCollection):
    """同じ入力集合からの完全な生成結果。モデル追加は候補政策を変更しない。"""


class ModelInputSlice(_ModelCollection):
    """L3等の対象別入力。取得範囲外を未生成と取り違えて再選定に使わない。"""

    subjects: tuple[Identifier, ...]

    @model_validator(mode="after")
    def bounded_subjects(self) -> Self:
        if len(set(self.subjects)) != len(self.subjects):
            raise ValueError("input slice subjects must be unique")
        if self.subject_plan is not None and not set(self.subject_plan).issubset(self.subjects):
            raise ValueError("subject plan is outside the input slice scope")
        if any(model.subject not in self.subjects for model in self.models):
            raise ValueError("model is outside the input slice subjects")
        return self


@dataclass(frozen=True, slots=True)
class ModelGenerationStream:
    """共有入力を読み終えた一回消費の生成列。保存形式ではない。"""

    metadata: GenerationMetadata
    subjects: tuple[str, ...]
    models: Iterator[GeneratedModel]

    def materialize(self) -> ModelGeneration:
        """有限の読取・比較用。全社公開はpublish_streamで逐次消費する。"""
        return ModelGeneration(**self.metadata.model_dump(), models=tuple(self.models))
