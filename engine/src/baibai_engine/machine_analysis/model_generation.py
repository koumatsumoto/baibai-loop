"""モデル生成の共通外枠。各モデルの意味と候補政策をここへ集約しない。"""

from __future__ import annotations

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


class _ModelCollection(BaseModel):
    """固定入力とモデル内容。完全生成と読取sliceが共有する意味。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    generation_id: Identifier
    as_of: date
    evaluation_time: AwareDatetime
    inputs: dict[Identifier, InputSnapshot] = Field(min_length=1)
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
        if any(model.subject not in self.subjects for model in self.models):
            raise ValueError("model is outside the input slice subjects")
        return self
