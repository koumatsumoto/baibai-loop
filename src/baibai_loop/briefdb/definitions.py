from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

DEFAULT_DEFINITIONS_PATH = Path(__file__).with_name("indicators.yaml")


@dataclass(frozen=True)
class SourceDefinition:
    source_id: str
    name: str
    provider: str | None = None
    tier: str | None = None
    url: str | None = None
    access_method: str | None = None
    status_policy: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class IndicatorDefinition:
    indicator_id: str
    name: str
    domain: str
    geography: str
    unit: str | None
    frequency: str
    primary_source_id: str | None = None
    transform_rule: str = "raw"
    description: str | None = None
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class CoverageRequirementDefinition:
    kind: str
    indicator_id: str
    required: bool = True
    max_staleness_days: int = 10


@dataclass(frozen=True)
class BriefDBDefinitions:
    sources: tuple[SourceDefinition, ...]
    indicators: tuple[IndicatorDefinition, ...]
    coverage_requirements: tuple[CoverageRequirementDefinition, ...]

    def alias_map(self) -> dict[str, IndicatorDefinition]:
        aliases: dict[str, IndicatorDefinition] = {}
        for indicator in self.indicators:
            aliases[indicator.name] = indicator
            for alias in indicator.aliases:
                aliases[alias] = indicator
        return aliases


def load_definitions(path: Path = DEFAULT_DEFINITIONS_PATH) -> BriefDBDefinitions:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"definition root must be a mapping: {path}")
    return BriefDBDefinitions(
        sources=tuple(_parse_source(entry) for entry in _list(raw.get("sources"))),
        indicators=tuple(_parse_indicator(entry) for entry in _list(raw.get("indicators"))),
        coverage_requirements=tuple(
            _parse_requirement(entry) for entry in _list(raw.get("coverage_requirements"))
        ),
    )


def _parse_source(raw: object) -> SourceDefinition:
    entry = _mapping(raw, "source")
    return SourceDefinition(
        source_id=_required_str(entry, "source_id"),
        name=_required_str(entry, "name"),
        provider=_optional_str(entry, "provider"),
        tier=_optional_str(entry, "tier"),
        url=_optional_str(entry, "url"),
        access_method=_optional_str(entry, "access_method"),
        status_policy=_optional_str(entry, "status_policy"),
        note=_optional_str(entry, "note"),
    )


def _parse_indicator(raw: object) -> IndicatorDefinition:
    entry = _mapping(raw, "indicator")
    return IndicatorDefinition(
        indicator_id=_required_str(entry, "indicator_id"),
        name=_required_str(entry, "name"),
        domain=_required_str(entry, "domain"),
        geography=_required_str(entry, "geography"),
        unit=_optional_str(entry, "unit"),
        frequency=_required_str(entry, "frequency"),
        primary_source_id=_optional_str(entry, "primary_source_id"),
        transform_rule=_optional_str(entry, "transform_rule") or "raw",
        description=_optional_str(entry, "description"),
        aliases=tuple(str(alias) for alias in _list(entry.get("aliases"))),
    )


def _parse_requirement(raw: object) -> CoverageRequirementDefinition:
    entry = _mapping(raw, "coverage requirement")
    max_staleness_days = entry.get("max_staleness_days", 10)
    if not isinstance(max_staleness_days, int):
        raise ValueError("max_staleness_days must be an integer")
    return CoverageRequirementDefinition(
        kind=_required_str(entry, "kind"),
        indicator_id=_required_str(entry, "indicator_id"),
        required=bool(entry.get("required", True)),
        max_staleness_days=max_staleness_days,
    )


def _mapping(raw: object, context: str) -> Mapping[str, object]:
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be a mapping")
    return cast(Mapping[str, object], raw)


def _list(raw: object) -> list[object]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("definition section must be a list")
    return cast(list[object], raw)


def _required_str(entry: Mapping[str, object], key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing required string field: {key}")
    return value


def _optional_str(entry: Mapping[str, object], key: str) -> str | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"field must be string when present: {key}")
    return value
