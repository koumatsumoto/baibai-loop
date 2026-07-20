"""Macro material-delta context loading and lightweight diagnostics."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.macro.models import MacroContextDocument


@dataclass(frozen=True, slots=True)
class MacroContext:
    context_id: str
    path: Path
    as_of: date
    valid_until: date
    payload: Mapping[str, Any]

    def is_stale_for(self, asof_date: date) -> bool:
        return self.valid_until < asof_date


def discover_macro_context_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.glob("*/*/macro-context-*.yaml") if path.is_file())


def find_latest_macro_context(root: Path, asof_date: date) -> Path | None:
    eligible = [
        path
        for path in discover_macro_context_files(root)
        if (parsed := _date_from_name(path.name)) is not None and parsed <= asof_date
    ]
    return eligible[-1] if eligible else None


def load_macro_context(path: Path) -> MacroContext:
    try:
        payload = safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"failed to load macro context: {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"macro context YAML root must be a mapping: {path}")
    return macro_context_from_payload(payload, source=path)


def macro_context_from_payload(
    payload: Mapping[str, Any],
    *,
    source: Path,
) -> MacroContext:
    try:
        document = MacroContextDocument.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "root"
        raise ValueError(
            f"macro context schema invalid at {location}: {first['msg']}: {source}"
        ) from exc
    return MacroContext(
        context_id=document.context_id,
        path=source,
        as_of=document.as_of,
        valid_until=document.valid_until,
        payload=document.payload(),
    )


def macro_context_diagnostics(
    context: MacroContext,
    *,
    asof_date: date,
) -> dict[str, object]:
    warnings: list[str] = []
    if context.as_of > asof_date:
        warnings.append("macro_context_future")
    if context.is_stale_for(asof_date):
        warnings.append("macro_context_stale")
    return {
        "context_id": context.context_id,
        "as_of": context.as_of.isoformat(),
        "valid_until": context.valid_until.isoformat(),
        "future": context.as_of > asof_date,
        "stale": context.is_stale_for(asof_date),
        "material_deltas": list(context_material_deltas(context.payload)),
        "sizing_cautions": list(context_sizing_cautions(context.payload)),
        "research_questions": list(context_research_questions(context.payload)),
        "refresh_triggers": list(context_refresh_triggers(context.payload)),
        "warnings": warnings,
    }


def context_material_deltas(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    return _section_items(payload, "material_deltas") or _mapping_sequence(
        payload.get("material_deltas")
    )


def context_sizing_cautions(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    return _section_items(payload, "sizing_cautions") or _mapping_sequence(
        payload.get("sizing_cautions")
    )


def context_research_questions(payload: Mapping[str, Any]) -> tuple[str, ...]:
    scenario = _section(payload, "scenarios_connections")
    if scenario is not None:
        connection = scenario.get("investment_connection")
        if isinstance(connection, Mapping):
            return _string_sequence(connection.get("research_priority_hints"))
    return _string_sequence(payload.get("research_questions"))


def context_refresh_triggers(payload: Mapping[str, Any]) -> tuple[str, ...]:
    monitoring = _section(payload, "monitoring_points")
    if monitoring is not None:
        return tuple(
            str(item["condition"])
            for item in _mapping_sequence(monitoring.get("monitoring_points"))
            if isinstance(item.get("condition"), str) and str(item["condition"]).strip()
        )
    return _string_sequence(payload.get("refresh_triggers"))


def _section_items(payload: Mapping[str, Any], key: str) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        item
        for section in _mapping_sequence(payload.get("sections"))
        for item in _mapping_sequence(section.get(key))
    )


def _section(payload: Mapping[str, Any], section_id: str) -> Mapping[str, Any] | None:
    return next(
        (
            section
            for section in _mapping_sequence(payload.get("sections"))
            if section.get("section_id") == section_id
        ),
        None,
    )


def _mapping_sequence(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _string_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item.strip())


def _required_str(payload: Mapping[str, Any], key: str, path: Path) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"macro context {key} must be a non-empty string: {path}")
    return value


def _required_date(payload: Mapping[str, Any], key: str, path: Path) -> date:
    raw = _required_str(payload, key, path)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"macro context {key} must be YYYY-MM-DD: {path}") from exc


def _required_mapping(payload: Mapping[str, Any], key: str, path: Path) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"macro context {key} must be a mapping: {path}")
    return value


def _required_list(payload: Mapping[str, Any], key: str, path: Path) -> list[Any]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ValueError(f"macro context {key} must be a list: {path}")
    return value


def _date_from_name(name: str) -> date | None:
    marker = "macro-context-"
    if marker not in name:
        return None
    raw = name.split(marker, 1)[1][:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None
