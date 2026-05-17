from __future__ import annotations

import calendar
import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import yaml

from .db import (
    ObservationRecord,
    initialize_database,
    insert_observation,
    upsert_indicator,
    upsert_source,
)
from .definitions import IndicatorDefinition, SourceDefinition, load_definitions

NUMERIC_CATEGORIES = ("market_indicators", "fx", "monthly_statistics", "policy_rates")
_NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


@dataclass(frozen=True)
class MigrationResult:
    brief_files: int
    sources: int
    indicators: int
    observations: int


@dataclass(frozen=True)
class NumericItem:
    layer_name: str
    category: str
    item: Mapping[str, object]


def migrate_briefs(
    root: Path,
    db_path: Path,
    *,
    brief_root: Path = Path("records/02-brief"),
) -> MigrationResult:
    definitions = load_definitions()
    alias_map = definitions.alias_map()
    conn = initialize_database(db_path, definitions=definitions)
    sources = 0
    indicators: set[str] = set()
    observations = 0
    brief_files = 0
    try:
        for path in sorted((root / brief_root).rglob("*.yaml")):
            document = _load_yaml(path)
            if not document:
                continue
            brief_files += 1
            source_index = _source_index(document)
            for source in source_index.values():
                upsert_source(conn, source)
                sources += 1
            for numeric_item in _iter_numeric_items(document):
                name = _item_name(numeric_item.item)
                indicator = alias_map.get(name) or _generated_indicator(numeric_item, name)
                upsert_indicator(conn, indicator)
                indicators.add(indicator.indicator_id)
                insert_observation(
                    conn,
                    _observation_from_item(
                        root,
                        path,
                        document,
                        numeric_item,
                        indicator,
                        source_index,
                    ),
                )
                observations += 1
        conn.commit()
    finally:
        conn.close()
    return MigrationResult(
        brief_files=brief_files,
        sources=sources,
        indicators=len(indicators),
        observations=observations,
    )


def parse_value_num(value: object) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str):
        return None
    match = _NUMBER_RE.search(value.replace("−", "-"))
    if match is None:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _load_yaml(path: Path) -> Mapping[str, object] | None:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return None
    return cast(Mapping[str, object], raw)


def _source_index(document: Mapping[str, object]) -> dict[str, SourceDefinition]:
    sources: dict[str, SourceDefinition] = {}
    for raw_source in _as_list(document.get("sources")):
        if not isinstance(raw_source, dict):
            continue
        source = cast(Mapping[str, object], raw_source)
        source_id = source.get("id")
        name = source.get("name")
        if not isinstance(source_id, str) or not isinstance(name, str):
            continue
        sources[source_id] = SourceDefinition(
            source_id=source_id,
            name=name,
            url=_optional_str(source.get("url")),
            note=_optional_str(source.get("note")),
        )
    return sources


def _iter_numeric_items(document: Mapping[str, object]) -> Iterable[NumericItem]:
    layers = document.get("layers")
    if not isinstance(layers, dict):
        return
    for layer_name, raw_layer in cast(Mapping[str, object], layers).items():
        if not isinstance(raw_layer, dict):
            continue
        layer = cast(Mapping[str, object], raw_layer)
        for category in NUMERIC_CATEGORIES:
            for item in _as_list(layer.get(category)):
                if isinstance(item, dict):
                    yield NumericItem(layer_name, category, cast(Mapping[str, object], item))


def _observation_from_item(
    root: Path,
    path: Path,
    document: Mapping[str, object],
    numeric_item: NumericItem,
    indicator: IndicatorDefinition,
    source_index: Mapping[str, SourceDefinition],
) -> ObservationRecord:
    item = numeric_item.item
    value = item.get("value")
    source_id = _first_source_id(item)
    source = source_index.get(source_id or "")
    period_start, period_end = _period_bounds(document)
    release_date = _optional_str(item.get("release_date"))
    as_of_date = _optional_str(item.get("as_of")) or period_end or release_date
    observation_date = _optional_str(document.get("observation_date"))
    fetch_status = _optional_str(item.get("fetch_status")) or (
        "ok" if value is not None else "failed"
    )
    return ObservationRecord(
        indicator_id=indicator.indicator_id,
        period_start=period_start,
        period_end=period_end,
        as_of_date=as_of_date,
        release_date=release_date,
        value_num=parse_value_num(value),
        value_text=None if value is None else str(value),
        unit=indicator.unit or _infer_unit(value),
        source_id=source_id,
        source_url=source.url if source else None,
        fetched_at=_source_accessed_at(source_id, document),
        vintage_at=observation_date,
        fetch_status=fetch_status,
        raw_payload_hash=None,
        brief_path=str(path.relative_to(root)),
        note=_optional_str(item.get("note")),
    )


def _period_bounds(document: Mapping[str, object]) -> tuple[str | None, str | None]:
    month = document.get("month")
    if isinstance(month, str) and re.fullmatch(r"\d{4}-\d{2}", month):
        year, month_num = (int(part) for part in month.split("-"))
        last_day = calendar.monthrange(year, month_num)[1]
        return f"{month}-01", f"{month}-{last_day:02d}"
    period = document.get("period")
    if isinstance(period, dict):
        start = _optional_str(cast(Mapping[str, object], period).get("start"))
        end = _optional_str(cast(Mapping[str, object], period).get("end"))
        return start, end
    return None, _optional_str(document.get("observation_date"))


def _source_accessed_at(source_id: str | None, document: Mapping[str, object]) -> str | None:
    if source_id is None:
        return None
    for raw_source in _as_list(document.get("sources")):
        if not isinstance(raw_source, dict):
            continue
        source = cast(Mapping[str, object], raw_source)
        if source.get("id") == source_id:
            accessed_at = _optional_str(source.get("accessed_at"))
            if accessed_at:
                return f"{accessed_at}T00:00:00Z"
    return datetime.now(UTC).isoformat()


def _generated_indicator(numeric_item: NumericItem, name: str) -> IndicatorDefinition:
    digest = hashlib.sha1(
        f"{numeric_item.layer_name}|{numeric_item.category}|{name}".encode()
    ).hexdigest()[:12]
    return IndicatorDefinition(
        indicator_id=f"brief-{digest}",
        name=name,
        domain=_domain_for_category(numeric_item.category),
        geography=numeric_item.layer_name,
        unit=_infer_unit(numeric_item.item.get("value")),
        frequency="monthly"
        if numeric_item.category in {"monthly_statistics", "policy_rates"}
        else "daily",
    )


def _domain_for_category(category: str) -> str:
    match category:
        case "fx":
            return "fx"
        case "policy_rates":
            return "policy"
        case "monthly_statistics":
            return "macro"
        case _:
            return "market"


def _infer_unit(value: object) -> str | None:
    if isinstance(value, str):
        if "bp" in value:
            return "bp"
        if "%" in value:
            return "percent"
        if "$" in value or "USD" in value:
            return "usd"
        if "円" in value or "Yen" in value:
            return "jpy"
    return None


def _item_name(item: Mapping[str, object]) -> str:
    name = item.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("numeric brief item is missing name")
    return name


def _first_source_id(item: Mapping[str, object]) -> str | None:
    source_ids = _as_list(item.get("source_ids"))
    first = source_ids[0] if source_ids else None
    return first if isinstance(first, str) else None


def _as_list(raw: object) -> list[object]:
    return cast(list[object], raw) if isinstance(raw, list) else []


def _optional_str(raw: object) -> str | None:
    return raw if isinstance(raw, str) and raw else None
