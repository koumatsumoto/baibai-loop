"""Canonical macro-context contract owned by the engine."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArticleInput(_StrictModel):
    input_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: HttpUrl
    published_at: datetime
    accessed_at: datetime
    status: Literal["ok", "failed"]
    used_for: str = Field(min_length=1)


class IndicatorSeriesInput(_StrictModel):
    input_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    series_id: str = Field(min_length=1)
    window: str = Field(min_length=1)
    observation_as_of: date
    published_at: datetime
    accessed_at: datetime
    status: Literal["ok", "failed"]
    used_for: str = Field(min_length=1)


class MacroInputs(_StrictModel):
    articles: tuple[ArticleInput, ...]
    indicator_series: tuple[IndicatorSeriesInput, ...]


class MaterialDelta(_StrictModel):
    channel: Literal["discount_rate", "demand", "funding", "common_tail"]
    direction: Literal["supportive", "adverse", "mixed"]
    materiality: Literal["low", "medium", "high"]
    summary: str = Field(min_length=1)
    used_for: str = Field(min_length=1)
    source_ids: tuple[str, ...] = Field(min_length=1)


class SizingCaution(_StrictModel):
    severity: Literal["low", "medium", "high"]
    summary: str = Field(min_length=1)
    source_ids: tuple[str, ...] = Field(min_length=1)


class MacroContextDocument(_StrictModel):
    schema_version: Literal[2]
    kind: Literal["macro-context"]
    context_id: str
    as_of: date
    valid_until: date
    published_at: datetime
    summary: str = Field(min_length=1)
    inputs: MacroInputs
    material_deltas: tuple[MaterialDelta, ...]
    sizing_cautions: tuple[SizingCaution, ...]
    research_questions: tuple[str, ...]
    refresh_triggers: tuple[str, ...]
    changes_since_previous: tuple[str, ...]

    @model_validator(mode="after")
    def validate_domain_contract(self) -> Self:
        if not re.fullmatch(r"macro-context-\d{4}-\d{2}-\d{2}-[a-z0-9-]+", self.context_id):
            raise ValueError("context_id has an invalid format")
        if self.valid_until < self.as_of:
            raise ValueError("valid_until must not predate as_of")
        if self.published_at.tzinfo is None:
            raise ValueError("published_at must include a timezone")
        if self.published_at.date() < self.as_of:
            raise ValueError("published_at must not predate as_of")
        statuses: dict[str, str] = {}
        for article in self.inputs.articles:
            if article.input_id in statuses:
                raise ValueError(f"input_id must be unique: {article.input_id}")
            statuses[article.input_id] = article.status
        for indicator in self.inputs.indicator_series:
            if indicator.input_id in statuses:
                raise ValueError(f"input_id must be unique: {indicator.input_id}")
            statuses[indicator.input_id] = indicator.status
        if not self.material_deltas and not self.sizing_cautions:
            raise ValueError("a material delta or sizing caution is required")
        for delta in self.material_deltas:
            missing = sorted(set(delta.source_ids) - statuses.keys())
            if missing:
                raise ValueError(f"references unknown input IDs: {', '.join(missing)}")
            if all(statuses[source_id] == "failed" for source_id in delta.source_ids):
                raise ValueError("an assessment cannot rely only on failed inputs")
        for caution in self.sizing_cautions:
            missing = sorted(set(caution.source_ids) - statuses.keys())
            if missing:
                raise ValueError(f"references unknown input IDs: {', '.join(missing)}")
            if all(statuses[source_id] == "failed" for source_id in caution.source_ids):
                raise ValueError("an assessment cannot rely only on failed inputs")
        for values, field_name in (
            (self.research_questions, "research_questions"),
            (self.refresh_triggers, "refresh_triggers"),
            (self.changes_since_previous, "changes_since_previous"),
        ):
            if any(not value.strip() for value in values):
                raise ValueError(f"{field_name} values must be non-empty")
        return self

    def payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")


__all__ = ["MacroContextDocument"]
