from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from baibai_engine.foundation.yaml_io import strict_safe_load

# Series definitions are split across one yaml per region so no single file grows
# unmanageable as the registry scales; the loader globs and merges them.
DEFAULT_REGISTRY_DIR = Path(__file__).with_name("registry")
_TRADINGVIEW_SYMBOL_RE = re.compile(r"[A-Za-z0-9._-]+:[A-Za-z0-9._!/-]+\Z")
# Append one higher generation and its membership digest whenever canonical
# series IDs change. Keeping prior entries lets stale branches identify
# themselves without consulting git history or a network service.
_REGISTRY_MEMBERSHIP_GENERATIONS = {
    "216243143e489d470030896183313ff0ceedffcbc6f476ad1cd2a2f849e6687c": 1,
    "7f4cee72f7eb5ac52cfea6862472d58fe33e71a9c3e9df6ccb8e3ab8ac443a5d": 2,
    "26e66a2dbe11090a3ebe7badd216899d679ae1a93c0ee9b6a6f9a773718eeffc": 3,
}


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
    plausible_min: float | None = None
    plausible_max: float | None = None
    aliases: tuple[str, ...] = ()
    tradingview_symbol: str | None = None

    def __post_init__(self) -> None:
        for field, value in (
            ("plausible_min", self.plausible_min),
            ("plausible_max", self.plausible_max),
        ):
            _validate_plausible_bound(field, value)
        if (
            self.plausible_min is not None
            and self.plausible_max is not None
            and self.plausible_min > self.plausible_max
        ):
            raise ValueError("plausible_min must be less than or equal to plausible_max")


def _validate_plausible_bound(field: str, value: object) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
    ):
        raise ValueError(f"{field} must be a finite number")


@dataclass(frozen=True)
class IndicatorDefinitions:
    series: tuple[SeriesDefinition, ...]
    generation: int = 1

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


def load_definitions(path: Path = DEFAULT_REGISTRY_DIR) -> IndicatorDefinitions:
    """Load series definitions from a registry directory (glob + merge) or a file.

    A directory merges every ``*.yaml`` in sorted order so region files combine
    into one registry; a file loads that single file (tests and ad-hoc checks).
    Duplicate ``series_id`` across files is a fatal registry error.
    """

    files = sorted(path.glob("*.yaml")) if path.is_dir() else [path]
    if not files:
        raise ValueError(f"indicator registry directory has no yaml files: {path}")
    series: list[SeriesDefinition] = []
    for file in files:
        raw = strict_safe_load(file.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"indicator definitions root must be a mapping: {file}")
        series.extend(_parse_series(item) for item in _list(raw.get("series")))
    _lint_unique_series_ids(series)
    _lint_alias_identity_collisions(series)
    generation = _canonical_registry_generation(series) if path == DEFAULT_REGISTRY_DIR else 1
    return IndicatorDefinitions(series=tuple(series), generation=generation)


def _canonical_registry_generation(series: list[SeriesDefinition]) -> int:
    membership = "\n".join(sorted(item.series_id for item in series)) + "\n"
    digest = hashlib.sha256(membership.encode()).hexdigest()
    generation = _REGISTRY_MEMBERSHIP_GENERATIONS.get(digest)
    if generation is None:
        raise ValueError(
            f"indicator registry membership changed without a new generation digest: {digest}"
        )
    return generation


def _lint_unique_series_ids(series: list[SeriesDefinition]) -> None:
    duplicates = sorted(
        series_id
        for series_id, count in Counter(item.series_id for item in series).items()
        if count > 1
    )
    if duplicates:
        raise ValueError(f"duplicate indicator series_id across registry: {', '.join(duplicates)}")


def _lint_alias_identity_collisions(series: list[SeriesDefinition]) -> None:
    canonical_owners: dict[str, set[str]] = {}
    for item in series:
        canonical_owners.setdefault(item.series_id.casefold(), set()).add(item.series_id)
        canonical_owners.setdefault(item.name.casefold(), set()).add(item.series_id)

    collisions: list[str] = []
    for item in series:
        for alias in item.aliases:
            other_owners = canonical_owners.get(alias.casefold(), set()) - {item.series_id}
            if other_owners:
                collisions.append(
                    f"{item.series_id}:{alias!r} -> {', '.join(sorted(other_owners))}"
                )
    if collisions:
        raise ValueError(
            "indicator alias collides with another series_id or name: "
            + "; ".join(sorted(collisions))
        )


def _parse_series(raw: object) -> SeriesDefinition:
    entry = _mapping(raw, "series")
    plausible_min = _optional_float(entry, "plausible_min")
    plausible_max = _optional_float(entry, "plausible_max")
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
        plausible_min=plausible_min,
        plausible_max=plausible_max,
        aliases=tuple(str(alias) for alias in _list(entry.get("aliases"))),
        tradingview_symbol=_optional_tradingview_symbol(entry),
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
        raise ValueError(f"indicator definition field {key!r} must be a non-empty string")
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
    raise ValueError(f"indicator definition field {key!r} must be an integer")


def _optional_float(entry: Mapping[str, object], key: str) -> float | None:
    value = entry.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"indicator definition field {key!r} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"indicator definition field {key!r} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"indicator definition field {key!r} must be a finite number")
    return number


def _optional_tradingview_symbol(entry: Mapping[str, object]) -> str | None:
    value = entry.get("tradingview_symbol")
    if value is None:
        return None
    if not isinstance(value, str) or not value or _TRADINGVIEW_SYMBOL_RE.fullmatch(value) is None:
        raise ValueError(
            "indicator definition field 'tradingview_symbol' must be a non-empty "
            "EXCHANGE:SYMBOL string"
        )
    return value
