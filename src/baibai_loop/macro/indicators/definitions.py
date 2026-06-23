from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from baibai_loop.foundation.yaml_io import safe_load

DEFAULT_DEFINITIONS_PATH = Path(__file__).with_name("series.yaml")


@dataclass(frozen=True)
class SeriesDefinition:
    series_id: str
    name: str
    category: str
    geography: str
    frequency: str
    unit: str
    provider: str
    provider_series_id: str
    source_id: str
    source_url: str
    priority: int = 100
    notes: str | None = None
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class StatsDefinitions:
    series: tuple[SeriesDefinition, ...]

    def by_id(self) -> dict[str, SeriesDefinition]:
        return {item.series_id: item for item in self.series}

    def search(self, query: str) -> tuple[SeriesDefinition, ...]:
        needle = query.casefold()
        if not needle:
            return self.series
        matched: list[SeriesDefinition] = []
        for item in self.series:
            haystack = " ".join(
                (item.series_id, item.name, item.category, item.geography, *item.aliases)
            ).casefold()
            if needle in haystack:
                matched.append(item)
        return tuple(matched)


def load_definitions(path: Path = DEFAULT_DEFINITIONS_PATH) -> StatsDefinitions:
    raw = safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"stats definitions root must be a mapping: {path}")
    return StatsDefinitions(series=tuple(_parse_series(item) for item in _list(raw.get("series"))))


def _parse_series(raw: object) -> SeriesDefinition:
    entry = _mapping(raw, "series")
    return SeriesDefinition(
        series_id=_required_str(entry, "series_id"),
        name=_required_str(entry, "name"),
        category=_required_str(entry, "category"),
        geography=_required_str(entry, "geography"),
        frequency=_required_str(entry, "frequency"),
        unit=_required_str(entry, "unit"),
        provider=_required_str(entry, "provider"),
        provider_series_id=_required_str(entry, "provider_series_id"),
        source_id=_required_str(entry, "source_id"),
        source_url=_required_str(entry, "source_url"),
        priority=_optional_int(entry, "priority") or 100,
        notes=_optional_str(entry, "notes"),
        aliases=tuple(str(alias) for alias in _list(entry.get("aliases"))),
    )


def _mapping(raw: object, label: str) -> Mapping[str, object]:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} entry must be a mapping")
    return cast(Mapping[str, object], raw)


def _list(raw: object) -> list[object]:
    return cast(list[object], raw) if isinstance(raw, list) else []


def _required_str(entry: Mapping[str, object], key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"stats definition field {key!r} must be a non-empty string")
    return value


def _optional_str(entry: Mapping[str, object], key: str) -> str | None:
    value = entry.get(key)
    return value if isinstance(value, str) and value else None


def _optional_int(entry: Mapping[str, object], key: str) -> int | None:
    value = entry.get(key)
    if value is None:
        return None
    if isinstance(value, int):
        return value
    raise ValueError(f"stats definition field {key!r} must be an integer")
