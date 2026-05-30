from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from .errors import ValidationFinding

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "records" / "_schemas" / "market-data.json"


def _load_validator() -> Draft202012Validator:
    raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected schema root: {SCHEMA_PATH}")
    Draft202012Validator.check_schema(raw)
    return Draft202012Validator(raw, format_checker=FormatChecker())


_VALIDATOR = _load_validator()


def discover_market_data_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in [*root.rglob("*.yaml"), *root.rglob("*.yml")] if path.is_file())


def validate_market_data_file(path: Path) -> list[ValidationFinding]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="market-data.io",
                message=f"failed to read or parse fallback market data: {exc}",
            )
        ]
    if not isinstance(document, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="market-data.root",
                message="fallback market data root must be a mapping",
            )
        ]
    findings: list[ValidationFinding] = []
    for error in _VALIDATOR.iter_errors(document):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=f"market-data.{error.validator or 'invalid'}",
                message=str(error.message),
                location=_format_path(error.absolute_path),
            )
        )
    findings.extend(_check_observation_quality(path, document))
    return findings


def _check_observation_quality(
    path: Path, document: Mapping[object, object]
) -> list[ValidationFinding]:
    observations = document.get("observations")
    if not isinstance(observations, list):
        return []
    findings: list[ValidationFinding] = []
    for index, item in enumerate(observations):
        if not isinstance(item, Mapping):
            continue
        provisional = item.get("provisional") is True
        if item.get("corporate_action_checked") is False and not provisional:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="market-data.corporate-action-provisional",
                    message="corporate_action_checked: false requires provisional: true",
                    location=f"observations[{index}].provisional",
                )
            )
        if item.get("price_basis") == "intraday_last" and not provisional:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="market-data.intraday-provisional",
                    message="price_basis: intraday_last requires provisional: true",
                    location=f"observations[{index}].provisional",
                )
            )
    return findings


def _format_path(parts: Iterable[Any]) -> str:
    rendered = "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in parts)
    return rendered.lstrip(".")
