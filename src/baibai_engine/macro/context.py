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
    try:
        document = MacroContextDocument.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "root"
        raise ValueError(
            f"macro context schema invalid at {location}: {first['msg']}: {path}"
        ) from exc
    return MacroContext(
        context_id=document.context_id,
        path=path,
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
        "material_deltas": list(_mapping_sequence(context.payload.get("material_deltas"))),
        "sizing_cautions": list(_mapping_sequence(context.payload.get("sizing_cautions"))),
        "warnings": warnings,
    }


def _mapping_sequence(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


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
