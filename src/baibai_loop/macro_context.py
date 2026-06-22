"""Macro context loading and lightweight diagnostics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from baibai_loop.foundation.yaml_io import safe_load


@dataclass(frozen=True, slots=True)
class MacroContext:
    context_id: str
    path: Path
    as_of: date
    valid_until: date
    payload: Mapping[str, Any]
    items: tuple[Mapping[str, Any], ...]

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
    payload = safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"macro context YAML root must be a mapping: {path}")
    kind = payload.get("kind")
    if kind != "macro-context":
        raise ValueError(f"macro context kind must be macro-context: {path}")
    context_id = _required_str(payload, "context_id", path)
    as_of = _required_date(payload, "as_of", path)
    valid_until = _required_date(payload, "valid_until", path)
    items = tuple(_iter_context_items(payload))
    return MacroContext(
        context_id=context_id,
        path=path,
        as_of=as_of,
        valid_until=valid_until,
        payload=payload,
        items=items,
    )


def macro_context_diagnostics(
    context: MacroContext,
    *,
    asof_date: date,
    candidate_sector: str | None = None,
) -> dict[str, object]:
    warnings: list[str] = []
    if context.as_of > asof_date:
        warnings.append("macro_context_future")
    if context.is_stale_for(asof_date):
        warnings.append("macro_context_stale")
    matched_items: list[dict[str, object]] = []
    unknown_items: list[dict[str, object]] = []
    for item in context.items:
        scope = str(item.get("scope") or "")
        key = str(item.get("key") or "")
        summary = {
            "id": item.get("id"),
            "scope": scope,
            "key": key,
            "stance": item.get("stance"),
            "strength": item.get("strength"),
            "confidence": item.get("confidence"),
        }
        if scope == "sector_33" and candidate_sector and key == candidate_sector:
            matched_items.append(summary)
        elif scope != "sector_33":
            unknown_items.append(summary)
    if unknown_items:
        warnings.append("macro_context_unknown_scope")
    return {
        "context_id": context.context_id,
        "as_of": context.as_of.isoformat(),
        "valid_until": context.valid_until.isoformat(),
        "future": context.as_of > asof_date,
        "stale": context.is_stale_for(asof_date),
        "matched_items": matched_items,
        "unknown_items": unknown_items,
        "warnings": warnings,
    }


def _iter_context_items(payload: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    items: list[Mapping[str, Any]] = []
    for block_name in ("sector_tilts",):
        block = payload.get(block_name)
        if not isinstance(block, Mapping):
            continue
        raw_items = block.get("items")
        if not isinstance(raw_items, list):
            continue
        for item in raw_items:
            if isinstance(item, Mapping):
                items.append(item)
    return items


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


def _date_from_name(name: str) -> date | None:
    marker = "macro-context-"
    if marker not in name:
        return None
    raw = name.split(marker, 1)[1][:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
