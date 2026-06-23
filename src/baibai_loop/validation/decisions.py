from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from baibai_loop.position.io import validate_decision_register_jsonl

from .errors import ValidationFinding

SCHEMA_ROOT = Path(__file__).resolve().parents[3] / "records" / "_schemas"


def discover_decisions_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in (root / "thesis-decisions").glob("*.jsonl") if path.is_file())


def validate_decisions_file(path: Path) -> list[ValidationFinding]:
    validator = _load_validator(SCHEMA_ROOT / "decision.json")
    findings: list[ValidationFinding] = []
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="ledger.io",
                message=f"failed to read file: {exc}",
            )
        ]
    parse_errors = False
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            parse_errors = True
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="ledger.invalid-json",
                    message=str(exc),
                    location=f"line {line_number}",
                )
            )
            continue
        if not isinstance(record, dict):
            parse_errors = True
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="ledger.non-object",
                    message="JSONL line must be an object",
                    location=f"line {line_number}",
                )
            )
            continue
        records.append(record)
        for error in validator.iter_errors(record):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code=f"ledger.{error.validator or 'invalid'}",
                    message=str(error.message),
                    location=f"line {line_number}:{_format_path(error.absolute_path)}",
                )
            )
        findings.extend(_check_tracking_mode(path, line_number, record))
    if not parse_errors:
        for message in validate_decision_register_jsonl(path):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="ledger.decision-register",
                    message=message,
                )
            )
    return findings


def _check_tracking_mode(
    path: Path, line_number: int, record: dict[str, Any]
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    tracking = record.get("tracking")
    if not isinstance(tracking, dict) or tracking.get("mode") not in {
        "post_approval",
        "re_examination",
        "none",
    }:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="ledger.tracking-mode",
                message="decision register rows require tracking.mode",
                location=f"line {line_number}.tracking.mode",
            )
        )
    return findings


def _load_validator(path: Path) -> Draft202012Validator:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected schema root: {path}")
    Draft202012Validator.check_schema(raw)
    return Draft202012Validator(raw, format_checker=FormatChecker())


def _format_path(parts: Iterable[Any]) -> str:
    rendered: list[str] = []
    for part in parts:
        rendered.append(
            f"[{part}]" if isinstance(part, int) else f".{part}" if rendered else str(part)
        )
    return "".join(rendered)
