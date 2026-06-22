"""Validate macro analysis YAML files."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from baibai_loop.macro_context import parse_datetime
from baibai_loop.stats.definitions import load_definitions
from baibai_loop.yaml_io import safe_load

from .errors import ValidationFinding

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "records" / "_schemas" / "macro-analysis.json"


def discover_macro_analysis_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.glob("*/*/macro-analysis-*.yaml") if path.is_file())


def validate_macro_analysis_file(path: Path) -> list[ValidationFinding]:
    try:
        payload = safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="macro-analysis.io",
                message=f"failed to read macro analysis: {exc}",
            )
        ]
    if not isinstance(payload, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="macro-analysis.root",
                message="macro analysis YAML root must be a mapping",
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
    return Draft202012Validator(raw)


_VALIDATOR = _load_validator()


def _validate_schema(path: Path, payload: Mapping[str, Any]) -> list[ValidationFinding]:
    return [
        ValidationFinding(
            severity="error",
            target=path,
            code=f"macro-analysis.{error.validator or 'invalid'}",
            message=str(error.message),
            location=".".join(str(part) for part in error.absolute_path),
        )
        for error in _VALIDATOR.iter_errors(payload)
    ]


def _validate_custom(path: Path, payload: Mapping[str, Any]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    if parse_datetime(payload.get("published_at")) is None:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="macro-analysis.published-at",
                message="published_at must be an ISO 8601 datetime",
                location="published_at",
            )
        )
    known_series = set(load_definitions().by_id())
    data_inputs = payload.get("data_inputs")
    if isinstance(data_inputs, list):
        for index, item in enumerate(data_inputs):
            if not isinstance(item, Mapping):
                continue
            series_id = item.get("series_id")
            if isinstance(series_id, str) and series_id not in known_series:
                findings.append(
                    ValidationFinding(
                        severity="warning",
                        target=path,
                        code="macro-analysis.unknown-series",
                        message=f"data_inputs series_id not in stats registry: {series_id}",
                        location=f"data_inputs.{index}.series_id",
                    )
                )
    return findings
