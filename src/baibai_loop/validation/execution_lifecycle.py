"""Validate the execution lifecycle contract for manual broker activity."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from baibai_loop.foundation.errors import ValidationFinding
from baibai_loop.foundation.yaml_io import safe_load
from baibai_loop.position.execution import (
    ExecutionLifecycleDocument,
    ExecutionLifecycleError,
    evaluate_execution_lifecycle,
)

SCHEMA_PATH = (
    Path(__file__).resolve().parents[3] / "records" / "_schemas" / "execution-lifecycle.json"
)


def discover_execution_lifecycle_files(root: Path) -> list[Path]:
    """Find forward execution records without scanning unrelated position artifacts."""

    return sorted(root.rglob("*-execution.yaml"))


def _load_validator() -> Draft202012Validator:
    raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected schema root: {SCHEMA_PATH}")
    Draft202012Validator.check_schema(raw)
    return Draft202012Validator(raw)


_VALIDATOR = _load_validator()


def validate_execution_lifecycle_file(
    path: Path, *, board_lot: int = 100
) -> list[ValidationFinding]:
    """Validate schema and lifecycle invariants for one staged contract fixture."""

    raw = _load_yaml(path)
    if isinstance(raw, list):
        return raw
    findings = _validate_schema(path, raw)
    if findings:
        return findings
    try:
        document = ExecutionLifecycleDocument.model_validate(raw)
        evaluate_execution_lifecycle(document, board_lot=board_lot)
    except (ExecutionLifecycleError, ValidationError, ValueError) as error:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="execution-lifecycle.reconciliation",
                message=str(error),
            )
        ]
    return []


def _load_yaml(path: Path) -> Mapping[str, object] | list[ValidationFinding]:
    try:
        raw = safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="execution-lifecycle.io",
                message=f"failed to read file: {error}",
            )
        ]
    except yaml.YAMLError as error:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="execution-lifecycle.invalid-yaml",
                message=f"YAML parse failed: {error}",
            )
        ]
    if not isinstance(raw, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="execution-lifecycle.non-mapping",
                message="execution lifecycle root must be a mapping",
            )
        ]
    return raw


def _validate_schema(path: Path, document: Mapping[str, object]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for error in _VALIDATOR.iter_errors(document):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=f"execution-lifecycle.{error.validator or 'invalid'}",
                message=str(error.message),
                location=_format_path(error.absolute_path),
            )
        )
    return findings


def _format_path(parts: Iterable[Any]) -> str:
    rendered: list[str] = []
    for part in parts:
        rendered.append(
            f"[{part}]" if isinstance(part, int) else f".{part}" if rendered else str(part)
        )
    return "".join(rendered)
