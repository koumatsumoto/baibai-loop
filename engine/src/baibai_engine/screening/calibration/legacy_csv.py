"""Read-only adapter for a calibration cache written before the L2 contract.

A store built as per-cohort CSV files is still readable, so a cohort measured under
the current rules does not have to be recomputed just because the storage changed.
This adapter is a reader and nothing else: it never writes, and no build is produced
from it, so the published L2 build stays the only authority for a cohort.

Values arrive as text and are coerced to the row's declared types, then put through
the same invariant checks a published build is put through. A legacy file whose
values do not satisfy the contract is refused rather than read into a cohort.
"""

from __future__ import annotations

import csv
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any, get_args, get_type_hints

import yaml

from baibai_engine.foundation.yaml_io import safe_load

from .forward import ForwardReturnRow
from .panel import PanelRow
from .store import CalibrationCacheError, forward_row_from_mapping, panel_row_from_mapping


def legacy_panel_path(root: Path, asof: date) -> Path:
    return root / f"panel-{asof.isoformat()}.csv"


def legacy_panel_meta_path(root: Path, asof: date) -> Path:
    return root / f"panel-{asof.isoformat()}.meta.yaml"


def legacy_forward_path(root: Path, asof: date) -> Path:
    return root / f"forward-{asof.isoformat()}.csv"


def legacy_cohorts(root: Path) -> list[date]:
    """Every as-of a legacy cache holds, in order."""

    cohorts: list[date] = []
    for path in sorted(root.glob("panel-*.csv")):
        try:
            cohorts.append(date.fromisoformat(path.stem.removeprefix("panel-")))
        except ValueError as exc:
            raise CalibrationCacheError(f"legacy panel filename has no as-of: {path.name}") from exc
    return cohorts


def read_legacy_panel(root: Path, asof: date) -> list[PanelRow]:
    rows = _read_rows(legacy_panel_path(root, asof), PanelRow)
    return [panel_row_from_mapping(_typed(raw, PanelRow)) for raw in rows]


def read_legacy_forward(root: Path, asof: date) -> list[ForwardReturnRow]:
    rows = _read_rows(legacy_forward_path(root, asof), ForwardReturnRow)
    return [forward_row_from_mapping(_typed(raw, ForwardReturnRow)) for raw in rows]


def read_legacy_panel_meta(root: Path, asof: date) -> dict[str, Any]:
    path = legacy_panel_meta_path(root, asof)
    try:
        payload = safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CalibrationCacheError(f"legacy panel metadata is unreadable: {path.name}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("rules_hash"), str):
        raise CalibrationCacheError(f"legacy panel metadata is invalid: {path.name}")
    return dict(payload)


def _read_rows(path: Path, row_type: type) -> list[dict[str, str]]:
    if not path.is_file():
        raise CalibrationCacheError(f"legacy cohort file is absent: {path.name}")
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        expected = set(get_type_hints(row_type))
        missing = sorted(expected.difference(reader.fieldnames or ()))
        if missing:
            raise CalibrationCacheError(
                f"legacy cohort file is missing {', '.join(missing)}: {path.name}"
            )
        return [dict(raw) for raw in reader]


def _typed(raw: Mapping[str, str], row_type: type) -> dict[str, Any]:
    """Read one text row as the declared types, dropping columns the contract dropped."""

    hints = get_type_hints(row_type)
    return {name: _value(raw.get(name), annotation) for name, annotation in hints.items()}


def _value(text: str | None, annotation: Any) -> Any:
    members = [item for item in get_args(annotation) if item is not type(None)]
    optional = type(None) in get_args(annotation)
    target = members[0] if members else annotation
    if text is None:
        return None
    if text == "":
        # An empty cell is absence only where the field can be absent. A required
        # text field that holds "" is carrying a real value — the empty list of
        # threshold blocks reads as "measured, none fired", not as "not measured".
        return None if optional else ""
    if target is bool:
        if text not in {"true", "false"}:
            raise CalibrationCacheError(f"legacy boolean is not true or false: {text!r}")
        return text == "true"
    if target is float:
        return float(text)
    if target is int:
        return int(text)
    return text
