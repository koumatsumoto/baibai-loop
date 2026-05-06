"""Validator callable allow-list used by policy and trade checks."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

import yaml

from .errors import ValidationFinding

KNOWN_VALIDATOR_CALLABLE_IDS = {
    "earnings_straddle_window",
    "boj_eve_window",
    "fomc_eve_window",
    "no_margin_trading",
}
_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


def validate_callable_ids_file(path: Path) -> list[ValidationFinding]:
    try:
        text = path.read_text(encoding="utf-8")
        match = _FRONT_MATTER_RE.match(text)
        raw = yaml.safe_load(match.group(1) if match else text)
    except (OSError, yaml.YAMLError) as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="registry.parse",
                message=f"failed to read callable-id source: {exc}",
            )
        ]
    findings: list[ValidationFinding] = []
    _walk(path, raw, findings, ())
    return findings


def _walk(
    path: Path,
    value: object,
    findings: list[ValidationFinding],
    parts: tuple[str, ...],
) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            next_parts = (*parts, str(key))
            if key == "validator_callable_id":
                if child not in KNOWN_VALIDATOR_CALLABLE_IDS:
                    findings.append(
                        ValidationFinding(
                            severity="error",
                            target=path,
                            code="registry.unknown-callable",
                            message=f"unknown validator_callable_id: {child!r}",
                            location=".".join(next_parts),
                        )
                    )
            else:
                _walk(path, child, findings, next_parts)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk(path, child, findings, (*parts, f"[{index}]"))
