"""Validate macro context YAML files."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from baibai_loop.foundation.yaml_io import safe_load
from baibai_loop.macro_context import parse_datetime

from .errors import ValidationFinding

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "records" / "_schemas" / "macro-context.json"


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
    return Draft202012Validator(raw)


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
    if parse_datetime(payload.get("published_at")) is None:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="macro-context.published-at",
                message="published_at must be an ISO 8601 datetime",
                location="published_at",
            )
        )
    inputs = payload.get("inputs")
    has_article = False
    has_stat = False
    if isinstance(inputs, Mapping):
        has_article = bool(inputs.get("articles"))
        has_stat = bool(inputs.get("stats_series"))
    if not (has_article or has_stat):
        findings.append(
            ValidationFinding(
                severity="warning",
                target=path,
                code="macro-context.inputs-empty",
                message="macro context should include at least one article or stats input",
                location="inputs",
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


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
