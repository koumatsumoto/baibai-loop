"""Validate screened YAML artefacts against the central jsonschema.

Phase 1-A で screened は YAML 正本になった。R6 のコア schema として
`records/_schemas/screened-v1.json` で構造を中央集約し、本モジュールはその schema
で artefact を検証する。playbook 別 schema (本文 section 構造) は本
モジュールの対象外。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from .errors import ValidationFinding

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "records" / "_schemas" / "screened-v1.json"


def _load_validator() -> Draft202012Validator:
    raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected schema root: {SCHEMA_PATH}")
    Draft202012Validator.check_schema(raw)
    return Draft202012Validator(raw)


_VALIDATOR = _load_validator()


def validate_screened_file(path: Path) -> list[ValidationFinding]:
    """Validate a single screened YAML file and return all findings."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="screened.io",
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
                code="screened.invalid-yaml",
                message=f"YAML parse failed: {exc}",
            )
        ]
    if not isinstance(document, dict):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="screened.non-mapping",
                message="screened YAML root must be a mapping",
            )
        ]
    findings: list[ValidationFinding] = []
    for error in _VALIDATOR.iter_errors(document):
        validator_keyword = error.validator or "invalid"
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=f"screened.{validator_keyword}",
                message=str(error.message),
                location=_format_path(error.absolute_path),
            )
        )
    return findings


def discover_screened_files(root: Path) -> list[Path]:
    """Return all records/03-screened/*.yaml files under ``root`` in sorted order."""
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
