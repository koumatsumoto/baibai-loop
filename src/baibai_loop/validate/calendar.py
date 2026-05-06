"""Calendar snapshot validation."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from .errors import ValidationFinding


def discover_calendar_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.glob("**/*.yaml") if path.is_file())


def validate_calendar_file(path: Path) -> list[ValidationFinding]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="calendar.parse",
                message=f"failed to read calendar snapshot: {exc}",
            )
        ]
    if not isinstance(raw, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="calendar.root",
                message="calendar snapshot must be a mapping",
            )
        ]
    findings: list[ValidationFinding] = []
    for field in ("covered_from", "covered_until", "last_refreshed_at", "source_status"):
        if field not in raw:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="calendar.required",
                    message=f"missing calendar field: {field}",
                    location=field,
                )
            )
    return findings
