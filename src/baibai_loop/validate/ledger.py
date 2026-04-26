from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .errors import ValidationFinding

SCHEMA_ROOT = Path(__file__).resolve().parents[3] / "schemas"


def discover_ledger_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    files: list[Path] = []
    for subdir in ("paper", "skipped"):
        files.extend(sorted((root / subdir).glob("*.jsonl")))
    return sorted(path for path in files if path.is_file())


def validate_ledger_file(path: Path) -> list[ValidationFinding]:
    schema_name = (
        "ledger-skipped-v1.json" if "/skipped/" in path.as_posix() else "ledger-paper-v1.json"
    )
    validator = _load_validator(SCHEMA_ROOT / schema_name)
    findings: list[ValidationFinding] = []
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
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
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
    return findings


def _load_validator(path: Path) -> Draft202012Validator:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected schema root: {path}")
    Draft202012Validator.check_schema(raw)
    return Draft202012Validator(raw)


def _format_path(parts: Iterable[Any]) -> str:
    rendered: list[str] = []
    for part in parts:
        rendered.append(
            f"[{part}]" if isinstance(part, int) else f".{part}" if rendered else str(part)
        )
    return "".join(rendered)
