"""Validate macro context YAML files."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from baibai_engine.foundation.coerce import parse_datetime
from baibai_engine.foundation.errors import ValidationFinding
from baibai_engine.foundation.yaml_io import safe_load

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "records" / "_schemas" / "macro-context.json"
# Canonical macro-series registry. Read as data (not imported) so this validator
# stays inside the import-direction contract (validation must not import macro),
# the same way it reads the JSON schema above.
_SERIES_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "macro" / "indicators" / "series.yaml"


@lru_cache(maxsize=1)
def _registered_series_ids() -> frozenset[str]:
    """series.yaml に登録された series_id と alias の集合。macro-context の
    indicator_series.series_id がここに無ければ refresh / grounding で解決できない。"""
    raw = safe_load(_SERIES_REGISTRY_PATH.read_text(encoding="utf-8"))
    ids: set[str] = set()
    if isinstance(raw, Mapping):
        for series in raw.get("series") or []:
            if not isinstance(series, Mapping):
                continue
            series_id = series.get("series_id")
            if isinstance(series_id, str):
                ids.add(series_id)
            for alias in series.get("aliases") or []:
                if isinstance(alias, str):
                    ids.add(alias)
    return frozenset(ids)


def discover_macro_context_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.glob("*/*/macro-context-*.yaml") if path.is_file())


def validate_macro_context_file(path: Path) -> list[ValidationFinding]:
    try:
        payload = safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="macro-context.io",
                message=f"failed to read macro context: {exc}",
            )
        ]
    if not isinstance(payload, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="macro-context.root",
                message="macro context YAML root must be a mapping",
            )
        ]
    findings = _validate_schema(path, payload)
    findings.extend(_validate_custom(path, payload))
    return findings


def _load_validator() -> Draft202012Validator:
    raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected schema root: {SCHEMA_PATH}")
    Draft202012Validator.check_schema(raw)
    return Draft202012Validator(raw, format_checker=FormatChecker())


_VALIDATOR = _load_validator()


def _validate_schema(path: Path, payload: Mapping[str, Any]) -> list[ValidationFinding]:
    return [
        ValidationFinding(
            severity="error",
            target=path,
            code=f"macro-context.{error.validator or 'invalid'}",
            message=str(error.message),
            location=".".join(str(part) for part in error.absolute_path),
        )
        for error in _VALIDATOR.iter_errors(payload)
    ]


def _validate_custom(path: Path, payload: Mapping[str, Any]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    as_of = _parse_date(payload.get("as_of"))
    valid_until = _parse_date(payload.get("valid_until"))
    if as_of is not None and valid_until is not None and valid_until < as_of:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="macro-context.valid-until",
                message="valid_until must be on or after as_of",
                location="valid_until",
            )
        )
    published_at = parse_datetime(payload.get("published_at"))
    if published_at is None:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="macro-context.published-at",
                message="published_at must be an ISO 8601 datetime",
                location="published_at",
            )
        )
    elif as_of is not None and published_at.date() < as_of:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="macro-context.published-at-before-as-of",
                message="published_at must not predate as_of",
                location="published_at",
            )
        )
    inputs = payload.get("inputs")
    input_ids: set[str] = set()
    duplicate_input_ids: set[str] = set()
    input_statuses: dict[str, str] = {}
    has_article = False
    has_indicator = False
    if isinstance(inputs, Mapping):
        articles = inputs.get("articles")
        indicator_series = inputs.get("indicator_series")
        has_article = bool(articles)
        has_indicator = bool(indicator_series)
        for collection in (articles, indicator_series):
            if not isinstance(collection, list):
                continue
            for item in collection:
                if not isinstance(item, Mapping):
                    continue
                input_id = item.get("input_id")
                if not isinstance(input_id, str):
                    continue
                if input_id in input_ids:
                    duplicate_input_ids.add(input_id)
                input_ids.add(input_id)
                status = item.get("status")
                if isinstance(status, str):
                    input_statuses[input_id] = status
    if not (has_article or has_indicator):
        findings.append(
            ValidationFinding(
                severity="warning",
                target=path,
                code="macro-context.inputs-empty",
                message="macro context should include at least one article or indicator input",
                location="inputs",
            )
        )
    for input_id in sorted(duplicate_input_ids):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="macro-context.duplicate-input-id",
                message=f"inputs.input_id must be unique: {input_id}",
                location="inputs",
            )
        )
    _validate_delta_sources(path, payload, input_ids, input_statuses, findings)
    if not payload.get("material_deltas") and not payload.get("sizing_cautions"):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="macro-context.material-delta-required",
                message="macro context requires a material delta or sizing caution",
                location="material_deltas",
            )
        )
    if isinstance(inputs, Mapping):
        indicator_series = inputs.get("indicator_series")
        if isinstance(indicator_series, list):
            registered = _registered_series_ids()
            for index, item in enumerate(indicator_series):
                series_id = item.get("series_id") if isinstance(item, Mapping) else None
                if isinstance(series_id, str) and series_id not in registered:
                    findings.append(
                        ValidationFinding(
                            severity="warning",
                            target=path,
                            code="macro-context.unregistered-indicator-series",
                            message=(
                                f"indicator_series.series_id '{series_id}' is not a registered "
                                "macro series (macro/indicators/series.yaml); refresh / grounding "
                                "cannot resolve it"
                            ),
                            location=f"inputs.indicator_series[{index}].series_id",
                        )
                    )
    if not payload.get("research_questions"):
        findings.append(
            ValidationFinding(
                severity="warning",
                target=path,
                code="macro-context.research-questions-empty",
                message="macro context should define research questions for stock analysis",
                location="research_questions",
            )
        )
    return findings


def _validate_delta_sources(
    path: Path,
    payload: Mapping[str, Any],
    input_ids: set[str],
    input_statuses: Mapping[str, str],
    findings: list[ValidationFinding],
) -> None:
    for field in ("material_deltas", "sizing_cautions"):
        items = payload.get(field)
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                continue
            source_ids = item.get("source_ids")
            if not isinstance(source_ids, list):
                continue
            resolved_ids = [source_id for source_id in source_ids if isinstance(source_id, str)]
            unknown = sorted(set(resolved_ids) - input_ids)
            if unknown:
                findings.append(
                    ValidationFinding(
                        severity="error",
                        target=path,
                        code="macro-context.unknown-source-id",
                        message=f"{field}[{index}] references unknown input IDs: {unknown}",
                        location=f"{field}[{index}].source_ids",
                    )
                )
            if resolved_ids and all(
                input_statuses.get(source_id) == "failed" for source_id in resolved_ids
            ):
                findings.append(
                    ValidationFinding(
                        severity="error",
                        target=path,
                        code="macro-context.failed-source-only",
                        message=f"{field}[{index}] cannot rely only on failed inputs",
                        location=f"{field}[{index}].source_ids",
                    )
                )


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
