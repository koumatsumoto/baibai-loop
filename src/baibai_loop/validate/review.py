from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from .errors import ValidationFinding

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "review-v1.json"
REQUIRED_SECTIONS: tuple[str, ...] = (
    "Outcome",
    "Hypothesis check",
    "Process check",
    "Lessons",
    "Next actions",
)
_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


def _load_schema() -> dict[str, Any]:
    raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected schema root: {SCHEMA_PATH}")
    Draft202012Validator.check_schema(raw)
    return raw


def _classifications_from_schema(schema: dict[str, Any]) -> tuple[str, ...]:
    # schema を単一の source of truth として扱う。constant 側を更新し忘れる drift を避ける。
    enum = schema["properties"]["classification"]["enum"]
    return tuple(str(value) for value in enum)


_SCHEMA = _load_schema()
_VALIDATOR = Draft202012Validator(_SCHEMA)
KNOWN_CLASSIFICATIONS: tuple[str, ...] = _classifications_from_schema(_SCHEMA)


def discover_review_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        path for path in root.rglob("*.md") if path.is_file() and path.name != "template.md"
    )


def validate_review_file(path: Path) -> list[ValidationFinding]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="review.io",
                message=f"failed to read file: {exc}",
            )
        ]
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="review.no-front-matter",
                message="review markdown must start with YAML front matter",
            )
        ]
    try:
        front = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="review.invalid-yaml",
                message=f"front matter YAML parse failed: {exc}",
            )
        ]
    if not isinstance(front, dict):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="review.front-matter-non-mapping",
                message="review front matter must be a mapping",
            )
        ]
    findings: list[ValidationFinding] = []
    for error in _VALIDATOR.iter_errors(front):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=f"review.{error.validator or 'invalid'}",
                message=str(error.message),
                location=_format_path(error.absolute_path),
            )
        )
    body = match.group(2)
    for section in REQUIRED_SECTIONS:
        if not re.search(rf"^##\s+{re.escape(section)}\s*$", body, flags=re.MULTILINE):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="review.missing-section",
                    message=f"required section missing: {section}",
                    location=section,
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
