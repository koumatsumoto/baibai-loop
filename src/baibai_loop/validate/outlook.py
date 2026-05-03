"""Validate outlook YAML artefacts against records/_schemas/outlook-v1.json."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from .errors import ValidationFinding

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "records" / "_schemas" / "outlook-v1.json"


def _load_validator() -> Draft202012Validator:
    raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected schema root: {SCHEMA_PATH}")
    Draft202012Validator.check_schema(raw)
    return Draft202012Validator(raw)


_VALIDATOR = _load_validator()


def validate_outlook_file(path: Path) -> list[ValidationFinding]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="outlook.io",
                message=f"failed to read file: {exc}",
            )
        ]
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="outlook.invalid-yaml",
                message=f"YAML parse failed: {exc}",
            )
        ]
    if not isinstance(document, dict):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="outlook.non-mapping",
                message="outlook YAML root must be a mapping",
            )
        ]
    findings: list[ValidationFinding] = []
    for error in _VALIDATOR.iter_errors(document):
        validator_keyword = error.validator or "invalid"
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=f"outlook.{validator_keyword}",
                message=str(error.message),
                location=_format_path(error.absolute_path),
            )
        )
    return findings


def discover_outlook_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.yaml") if p.is_file())


def _format_path(parts: Iterable[object]) -> str:
    parts_list = list(parts)
    if not parts_list:
        return ""
    rendered: list[str] = []
    for part in parts_list:
        if isinstance(part, int):
            rendered.append(f"[{part}]")
        else:
            rendered.append(f".{part}" if rendered else str(part))
    return "".join(rendered)
