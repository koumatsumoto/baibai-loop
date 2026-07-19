"""Validate persisted portfolio outcomes against their source artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, cast

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from baibai_engine.foundation.errors import ValidationFinding
from baibai_engine.foundation.yaml_io import safe_load

SCHEMA_PATH = (
    Path(__file__).resolve().parents[3] / "records" / "_schemas" / "portfolio-outcome.json"
)


def _validator() -> Draft202012Validator:
    raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return Draft202012Validator(raw, format_checker=FormatChecker())


_VALIDATOR = _validator()


def discover_portfolio_outcome_files(root: Path) -> list[Path]:
    outcomes = root / "outcomes"
    return (
        sorted(path for path in outcomes.rglob("*.yaml") if path.is_file())
        if outcomes.exists()
        else []
    )


def validate_portfolio_outcome_file(path: Path) -> list[ValidationFinding]:
    try:
        raw = safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        return [_finding(path, "portfolio-outcome.io", str(error))]
    if not isinstance(raw, Mapping):
        return [_finding(path, "portfolio-outcome.root", "root must be a mapping")]
    findings = [
        _finding(
            path,
            f"portfolio-outcome.{error.validator or 'invalid'}",
            error.message,
            _path(error.absolute_path),
        )
        for error in _VALIDATOR.iter_errors(raw)
    ]
    if findings:
        return findings
    root = _repository_root(path)
    for ref_key, hash_key in (
        ("ledger_ref", "ledger_sha256"),
        ("benchmark_observation_ref", "benchmark_observation_sha256"),
        ("market_data_ref", "market_data_sha256"),
    ):
        reference = raw[ref_key]
        expected = raw[hash_key]
        if not isinstance(reference, str):
            continue
        source = (
            (root / reference).resolve() if not Path(reference).is_absolute() else Path(reference)
        )
        if not source.is_file():
            return [_finding(path, "portfolio-outcome.source", f"source is missing: {reference}")]
        if expected is None and ref_key == "market_data_ref":
            continue
        if not isinstance(expected, str):
            return [
                _finding(path, "portfolio-outcome.source", f"source hash is missing: {reference}")
            ]
        actual = hashlib.sha256(source.read_bytes()).hexdigest()
        if actual != expected:
            return [
                _finding(path, "portfolio-outcome.source", f"source hash mismatch: {reference}")
            ]
    semantic_error = _semantic_error(raw, root)
    if semantic_error is None:
        return []
    return [_finding(path, "portfolio-outcome.invalid", semantic_error)]


def _semantic_error(raw: Mapping[str, object], root: Path) -> str | None:
    if raw.get("status") != "resolved":
        return None
    try:
        reference = raw["benchmark_observation_ref"]
        assert isinstance(reference, str)
        benchmark = safe_load((root / reference).read_text(encoding="utf-8"))
        assert isinstance(benchmark, Mapping)
        for key in ("horizon", "period_start_date", "period_end_date", "benchmark_id"):
            if raw.get(key) != benchmark.get(key):
                return f"outcome {key} does not match benchmark observation"
        if raw.get("benchmark_cumulative_return_pct") != benchmark.get("cumulative_return_pct"):
            return "outcome benchmark return does not match benchmark observation"
        portfolio = raw.get("portfolio_twr_pct")
        benchmark_return = raw.get("benchmark_cumulative_return_pct")
        excess = raw.get("excess_percentage_points")
        values = (portfolio, benchmark_return, excess)
        if not all(isinstance(value, int | float) for value in values):
            return "resolved outcome return fields must be numeric"
        portfolio, benchmark_return, excess = cast(tuple[float, float, float], values)
        if abs((float(portfolio) - float(benchmark_return)) - float(excess)) > 1e-9:
            return "excess_percentage_points does not equal portfolio minus benchmark"
    except (AssertionError, OSError, ValueError, KeyError):
        return "outcome benchmark source cannot be validated"
    return None


def _repository_root(path: Path) -> Path:
    records = next((ancestor for ancestor in path.parents if ancestor.name == "records"), None)
    if records is None:
        raise RuntimeError(f"outcome is not below a records directory: {path}")
    return records.parent


def _finding(path: Path, code: str, message: str, location: str | None = None) -> ValidationFinding:
    return ValidationFinding("error", path, code, message, location)


def _path(parts: Iterable[Any]) -> str:
    return ".".join(str(part) for part in parts)
