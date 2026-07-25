"""Canonical macro-context contract owned by the engine."""

from __future__ import annotations

import re
from datetime import date, datetime
from functools import lru_cache
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from baibai_engine.macro.indicators.definitions import load_definitions

type MacroSectionId = Literal[
    "regime_summary",
    "rates_policy",
    "growth_demand",
    "inflation_costs",
    "fx_liquidity",
    "japan_specific",
    "scenarios_connections",
    "monitoring_points",
]

SECTION_ORDER: tuple[MacroSectionId, ...] = (
    "regime_summary",
    "rates_policy",
    "growth_demand",
    "inflation_costs",
    "fx_liquidity",
    "japan_specific",
    "scenarios_connections",
    "monitoring_points",
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _SourcedStatement(_StrictModel):
    summary: str = Field(min_length=1)
    source_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("summary")
    @classmethod
    def require_non_blank_summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("summary must be non-blank")
        return value

    @field_validator("source_ids")
    @classmethod
    def require_unique_non_blank_sources(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("source_ids must be non-blank")
        if len(values) != len(set(values)):
            raise ValueError("source_ids must be unique")
        return values


class ArticleInput(_StrictModel):
    input_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: HttpUrl
    published_at: datetime
    accessed_at: datetime
    status: Literal["ok", "failed"]
    used_for: str = Field(min_length=1)

    @field_validator("published_at", "accessed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("macro input datetime must include a timezone")
        return value


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

    @field_validator("published_at", "accessed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("macro input datetime must include a timezone")
        return value


class MacroInputs(_StrictModel):
    articles: tuple[ArticleInput, ...]
    indicator_series: tuple[IndicatorSeriesInput, ...]


class FactSummary(_SourcedStatement):
    pass


class SectionJudgment(_SourcedStatement):
    direction: Literal["supportive", "adverse", "mixed"]
    confidence: Literal["low", "medium", "high"]


class MaterialDelta(_SourcedStatement):
    channel: Literal["discount_rate", "demand", "funding", "common_tail"]
    direction: Literal["supportive", "adverse", "mixed"]
    materiality: Literal["low", "medium", "high"]
    used_for: str = Field(min_length=1)

    @field_validator("used_for")
    @classmethod
    def require_non_blank_use(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("used_for must be non-blank")
        return value


class SizingCaution(_SourcedStatement):
    severity: Literal["low", "medium", "high"]


class InvestmentConnection(_SourcedStatement):
    sector_tilts: tuple[str, ...] = ()
    research_priority_hints: tuple[str, ...] = ()

    @field_validator("sector_tilts", "research_priority_hints")
    @classmethod
    def require_non_blank_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("investment connection items must be non-blank")
        return values


class MacroScenario(_SourcedStatement):
    case: Literal["base", "bear", "bull"]
    direction: Literal["supportive", "adverse", "mixed"]
    conditions: tuple[str, ...] = Field(min_length=1)
    investment_implications: tuple[str, ...] = Field(min_length=1)

    @field_validator("conditions", "investment_implications")
    @classmethod
    def require_non_blank_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("scenario items must be non-blank")
        return values


class MonitoringPoint(_SourcedStatement):
    event: str = Field(min_length=1)
    condition: str = Field(min_length=1)
    view_change: str = Field(min_length=1)

    @field_validator("event", "condition", "view_change")
    @classmethod
    def require_non_blank_fields(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("monitoring point fields must be non-blank")
        return value


class MacroContextSection(_StrictModel):
    section_id: MacroSectionId
    series_ids: tuple[str, ...] = Field(min_length=1)
    fact_summary: tuple[FactSummary, ...] = Field(min_length=1)
    judgment: SectionJudgment
    investment_connection: InvestmentConnection
    change_since_previous: str | None = None
    material_deltas: tuple[MaterialDelta, ...] = ()
    sizing_cautions: tuple[SizingCaution, ...] = ()
    scenarios: tuple[MacroScenario, ...] = ()
    monitoring_points: tuple[MonitoringPoint, ...] = ()

    @model_validator(mode="after")
    def validate_section_contract(self) -> Self:
        if len(self.series_ids) != len(set(self.series_ids)):
            raise ValueError("section series_ids must be unique")
        if self.section_id == "regime_summary":
            if self.change_since_previous is None or not self.change_since_previous.strip():
                raise ValueError("regime summary requires a change from the previous context")
        elif self.change_since_previous is not None:
            raise ValueError("change from the previous context belongs in the regime summary")
        if self.section_id in {"regime_summary", "monitoring_points"} and (
            self.material_deltas or self.sizing_cautions
        ):
            raise ValueError("material deltas and sizing cautions belong in sections 2 through 7")
        if self.section_id == "scenarios_connections":
            if tuple(item.case for item in self.scenarios) != ("base", "bear", "bull"):
                raise ValueError("scenario section must contain base, bear, bull in that order")
            if not self.investment_connection.sector_tilts:
                raise ValueError("scenario section requires sector tilts")
            if not self.investment_connection.research_priority_hints:
                raise ValueError("scenario section requires research priority hints")
        else:
            if self.scenarios:
                raise ValueError("scenarios belong in the scenario section")
            if self.investment_connection.sector_tilts:
                raise ValueError("sector tilts belong in the scenario section")
            if self.investment_connection.research_priority_hints:
                raise ValueError("research priority hints belong in the scenario section")
        if self.section_id == "monitoring_points":
            if not self.monitoring_points:
                raise ValueError("monitoring section requires monitoring points")
        elif self.monitoring_points:
            raise ValueError("monitoring points belong in the monitoring section")
        return self


class MacroContextDocument(_StrictModel):
    schema_version: Literal[3]
    kind: Literal["macro-context"]
    context_id: str
    as_of: date
    valid_until: date
    published_at: datetime
    summary: str = Field(min_length=1)
    inputs: MacroInputs
    sections: tuple[MacroContextSection, ...] = Field(min_length=8, max_length=8)

    @model_validator(mode="after")
    def validate_domain_contract(self) -> Self:
        if not re.fullmatch(r"macro-context-\d{4}-\d{2}-\d{2}-[a-z0-9-]+", self.context_id):
            raise ValueError("context_id has an invalid format")
        if self.valid_until < self.as_of:
            raise ValueError("valid_until must not predate as_of")
        if self.published_at.tzinfo is None or self.published_at.utcoffset() is None:
            raise ValueError("published_at must include a timezone")
        if self.published_at.date() < self.as_of:
            raise ValueError("published_at must not predate as_of")
        if tuple(section.section_id for section in self.sections) != SECTION_ORDER:
            raise ValueError("macro context must contain the fixed eight sections in order")

        statuses: dict[str, str] = {}
        series_input_ids: dict[str, set[str]] = {}
        for article in self.inputs.articles:
            if article.input_id in statuses:
                raise ValueError(f"input_id must be unique: {article.input_id}")
            statuses[article.input_id] = article.status
        for indicator in self.inputs.indicator_series:
            if indicator.input_id in statuses:
                raise ValueError(f"input_id must be unique: {indicator.input_id}")
            statuses[indicator.input_id] = indicator.status
            if indicator.series_id not in _canonical_series_ids():
                raise ValueError(f"unregistered macro series_id: {indicator.series_id}")
            series_input_ids.setdefault(indicator.series_id, set()).add(indicator.input_id)
        if not statuses:
            raise ValueError("at least one macro input is required")

        has_material_assessment = False
        for section in self.sections:
            section_source_ids = {
                source_id
                for item in (
                    *section.fact_summary,
                    section.judgment,
                    section.investment_connection,
                    *section.material_deltas,
                    *section.sizing_cautions,
                    *section.scenarios,
                    *section.monitoring_points,
                )
                for source_id in item.source_ids
            }
            for series_id in section.series_ids:
                if series_id not in _canonical_series_ids():
                    raise ValueError(f"unregistered macro series_id: {series_id}")
                if series_id not in series_input_ids:
                    raise ValueError(f"section series_id has no indicator input: {series_id}")
                ok_input_ids = {
                    input_id
                    for input_id in series_input_ids[series_id]
                    if statuses[input_id] == "ok"
                }
                if not ok_input_ids & section_source_ids:
                    raise ValueError(
                        f"section series_id has no cited successful indicator input: {series_id}"
                    )
            sourced_items: tuple[_SourcedStatement, ...] = (
                *section.fact_summary,
                section.judgment,
                section.investment_connection,
                *section.material_deltas,
                *section.sizing_cautions,
                *section.scenarios,
                *section.monitoring_points,
            )
            for item in sourced_items:
                missing = sorted(set(item.source_ids) - statuses.keys())
                if missing:
                    raise ValueError(f"references unknown input IDs: {', '.join(missing)}")
                if any(statuses[source_id] == "failed" for source_id in item.source_ids):
                    raise ValueError("an assessment cannot cite failed inputs")
            has_material_assessment |= bool(section.material_deltas or section.sizing_cautions)
        if not has_material_assessment:
            raise ValueError("a material delta or sizing caution is required")
        return self

    @property
    def material_deltas(self) -> tuple[MaterialDelta, ...]:
        return tuple(delta for section in self.sections for delta in section.material_deltas)

    @property
    def sizing_cautions(self) -> tuple[SizingCaution, ...]:
        return tuple(caution for section in self.sections for caution in section.sizing_cautions)

    @property
    def research_questions(self) -> tuple[str, ...]:
        section = self.sections[6]
        return section.investment_connection.research_priority_hints

    @property
    def refresh_triggers(self) -> tuple[str, ...]:
        return tuple(point.condition for point in self.sections[7].monitoring_points)

    def payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")


@lru_cache(maxsize=1)
def _canonical_series_ids() -> frozenset[str]:
    return frozenset(series.series_id for series in load_definitions().series)


__all__ = ["SECTION_ORDER", "MacroContextDocument", "MacroSectionId"]
