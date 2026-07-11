"""Macro material-delta context loading and lightweight diagnostics."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from baibai_loop.foundation.coerce import parse_datetime
from baibai_loop.foundation.yaml_io import safe_load

_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "records" / "_schemas" / "macro-context.json"


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
    _validate_runtime_contract(payload, path)
    kind = payload.get("kind")
    if kind != "macro-context":
        raise ValueError(f"macro context kind must be macro-context: {path}")
    if payload.get("schema_version") != 2:
        raise ValueError(f"macro context schema_version must be 2: {path}")
    context_id = _required_str(payload, "context_id", path)
    as_of = _required_date(payload, "as_of", path)
    valid_until = _required_date(payload, "valid_until", path)
    if valid_until < as_of:
        raise ValueError(f"macro context valid_until must not predate as_of: {path}")
    _required_str(payload, "summary", path)
    _required_mapping(payload, "inputs", path)
    for key in (
        "material_deltas",
        "sizing_cautions",
        "research_questions",
        "refresh_triggers",
        "changes_since_previous",
    ):
        _required_list(payload, key, path)
    return MacroContext(
        context_id=context_id,
        path=path,
        as_of=as_of,
        valid_until=valid_until,
        payload=payload,
    )


@lru_cache(maxsize=1)
def _schema_validator() -> Draft202012Validator:
    raw = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected macro context schema: {_SCHEMA_PATH}")
    return Draft202012Validator(raw, format_checker=FormatChecker())


def _validate_runtime_contract(payload: Mapping[str, Any], path: Path) -> None:
    errors = sorted(_schema_validator().iter_errors(payload), key=lambda error: list(error.path))
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "root"
        raise ValueError(f"macro context schema invalid at {location}: {error.message}: {path}")
    if parse_datetime(payload.get("published_at")) is None:
        raise ValueError(f"macro context published_at must be an ISO 8601 datetime: {path}")

    inputs = payload.get("inputs")
    if not isinstance(inputs, Mapping):
        return
    input_statuses: dict[str, str] = {}
    for collection_name in ("articles", "indicator_series"):
        collection = inputs.get(collection_name)
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, Mapping):
                continue
            input_id = item.get("input_id")
            status = item.get("status")
            if not isinstance(input_id, str) or not isinstance(status, str):
                continue
            if input_id in input_statuses:
                raise ValueError(f"macro context input_id must be unique: {input_id}: {path}")
            input_statuses[input_id] = status
    for field in ("material_deltas", "sizing_cautions"):
        entries = payload.get(field)
        if not isinstance(entries, list):
            continue
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                continue
            source_ids = entry.get("source_ids")
            if not isinstance(source_ids, list):
                continue
            missing = sorted(
                source_id
                for source_id in source_ids
                if isinstance(source_id, str) and source_id not in input_statuses
            )
            if missing:
                raise ValueError(
                    f"macro context {field}[{index}] references unknown input IDs: "
                    f"{missing}: {path}"
                )
            if source_ids and all(
                input_statuses.get(source_id) == "failed" for source_id in source_ids
            ):
                raise ValueError(
                    f"macro context {field}[{index}] cannot rely only on failed inputs: {path}"
                )
    if not payload.get("material_deltas") and not payload.get("sizing_cautions"):
        raise ValueError(f"macro context requires a material delta or sizing caution: {path}")


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
