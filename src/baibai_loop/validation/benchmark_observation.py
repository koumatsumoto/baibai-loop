"""Validate offline JPX gross-total-return benchmark observations."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from baibai_loop.foundation.benchmark_observation import benchmark_observation_error
from baibai_loop.foundation.errors import ValidationFinding
from baibai_loop.foundation.yaml_io import safe_load

SCHEMA_PATH = (
    Path(__file__).resolve().parents[3] / "records" / "_schemas" / "benchmark-observation.json"
)


def _validator() -> Draft202012Validator:
    raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected schema root: {SCHEMA_PATH}")
    return Draft202012Validator(raw, format_checker=FormatChecker())


_VALIDATOR = _validator()


def discover_benchmark_observation_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*.yaml") if path.is_file())


def validate_benchmark_observation_file(path: Path) -> list[ValidationFinding]:
    try:
        raw = safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        return [_finding(path, "benchmark-observation.io", str(error))]
    if not isinstance(raw, Mapping):
        return [_finding(path, "benchmark-observation.root", "root must be a mapping")]
    findings = [
        _finding(
            path,
            f"benchmark-observation.{error.validator or 'invalid'}",
            error.message,
            location=_path(error.absolute_path),
        )
        for error in _VALIDATOR.iter_errors(raw)
    ]
    if findings:
        return findings
    semantic_error = _semantic_error(raw)
    if semantic_error is not None:
        return [_finding(path, "benchmark-observation.invalid", semantic_error)]
    return []


def _semantic_error(raw: Mapping[str, object]) -> str | None:
    """Use the same pure contract as the runtime loader."""

    try:
        start = date.fromisoformat(str(raw["period_start_date"]))
        end = date.fromisoformat(str(raw["period_end_date"]))
        source_as_of = date.fromisoformat(str(raw["source_as_of"]))
        published_at = date.fromisoformat(str(raw["published_at"]))
        retrieved_at = datetime.fromisoformat(str(raw["retrieved_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return "period and publication fields must be ISO dates"
    horizon = raw.get("horizon")
    period_basis = raw.get("period_basis")
    cumulative = raw.get("cumulative_return_pct")
    annualized = raw.get("annualized_return_pct")
    precision = raw.get("display_precision_bps")
    if horizon not in {"1y", "3y", "5y"} or period_basis not in {
        "official_explicit",
        "official_month_end_rule",
    }:
        return "benchmark horizon or period basis is invalid"
    if not isinstance(cumulative, int | float) or not isinstance(precision, int):
        return "benchmark return or display precision is invalid"
    if annualized is not None and not isinstance(annualized, int | float):
        return "annualized_return_pct must be numeric or null"
    rule_url = raw.get("period_rule_source_url")
    return benchmark_observation_error(
        period_start_date=start,
        period_end_date=end,
        source_as_of=source_as_of,
        published_at=published_at,
        retrieved_at=retrieved_at,
        horizon=cast(Literal["1y", "3y", "5y"], horizon),
        period_basis=cast(Literal["official_explicit", "official_month_end_rule"], period_basis),
        period_rule_source_url=rule_url if isinstance(rule_url, str) else None,
        cumulative_return_pct=float(cumulative),
        annualized_return_pct=None if annualized is None else float(annualized),
        display_precision_bps=precision,
    )


def _finding(
    path: Path, code: str, message: str, *, location: str | None = None
) -> ValidationFinding:
    return ValidationFinding("error", path, code, message, location)


def _path(parts: Iterable[Any]) -> str:
    return ".".join(str(part) for part in parts)
