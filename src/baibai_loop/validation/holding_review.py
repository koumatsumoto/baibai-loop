"""Validate holding review drafts: schema, axis completeness, action agreement."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from baibai_loop.foundation.errors import ValidationFinding
from baibai_loop.foundation.yaml_io import safe_load
from baibai_loop.position.holding_review import (
    HoldingReviewDocument,
    HoldingReviewError,
    evaluate_holding_review,
    validate_holding_review_sources,
)
from baibai_loop.thesis.holding_review_builder import validate_holding_review_scalars

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "records" / "_schemas" / "holding-review.json"


def _load_validator() -> Draft202012Validator:
    raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected schema root: {SCHEMA_PATH}")
    Draft202012Validator.check_schema(raw)
    return Draft202012Validator(raw, format_checker=FormatChecker())


_VALIDATOR = _load_validator()


def discover_holding_review_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*-review.yaml") if path.is_file())


def validate_holding_review_file(path: Path) -> list[ValidationFinding]:
    raw = _load_yaml(path)
    if isinstance(raw, list):
        return raw
    findings = _validate_schema(path, raw)
    if findings:
        return findings
    try:
        document = HoldingReviewDocument.model_validate(raw)
        validate_holding_review_sources(document, root=Path.cwd())
        validate_holding_review_scalars(document, root=Path.cwd())
        result = evaluate_holding_review(document)
    except (ValueError, HoldingReviewError) as error:
        return [_finding(path, "error", "holding-review.invalid", str(error))]
    findings.extend(
        _finding(path, "error", "holding-review.incomplete", message) for message in result.errors
    )
    findings.extend(
        _finding(path, "warning", "holding-review.warning", message) for message in result.warnings
    )
    return findings


def _load_yaml(path: Path) -> Mapping[str, object] | list[ValidationFinding]:
    try:
        raw = safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        return [_finding(path, "error", "holding-review.io", str(error))]
    except yaml.YAMLError as error:
        return [_finding(path, "error", "holding-review.invalid-yaml", str(error))]
    if not isinstance(raw, Mapping):
        return [
            _finding(
                path,
                "error",
                "holding-review.non-mapping",
                "holding review root must be a mapping",
            )
        ]
    return raw


def _validate_schema(path: Path, document: Mapping[str, object]) -> list[ValidationFinding]:
    return [
        _finding(
            path,
            "error",
            f"holding-review.{error.validator or 'invalid'}",
            error.message,
            location=_format_path(error.absolute_path),
        )
        for error in _VALIDATOR.iter_errors(document)
    ]


def _finding(
    path: Path,
    severity: str,
    code: str,
    message: str,
    *,
    location: str | None = None,
) -> ValidationFinding:
    if severity not in {"error", "warning"}:
        raise ValueError(f"invalid finding severity: {severity}")
    return ValidationFinding(
        severity=severity,  # type: ignore[arg-type]
        target=path,
        code=code,
        message=message,
        location=location,
    )


def _format_path(parts: Iterable[Any]) -> str:
    rendered: list[str] = []
    for part in parts:
        rendered.append(
            f"[{part}]" if isinstance(part, int) else f".{part}" if rendered else str(part)
        )
    return "".join(rendered)
