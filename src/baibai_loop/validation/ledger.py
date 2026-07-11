"""Validate the repository-only portfolio ledger contract and reconciliation."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from baibai_loop.foundation.errors import ValidationFinding
from baibai_loop.foundation.yaml_io import safe_load
from baibai_loop.position.ledger import (
    CANONICAL_LEDGER_FILENAME,
    PortfolioLedgerDocument,
    PortfolioLedgerError,
    reconcile_portfolio,
)

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "records" / "_schemas" / "portfolio-ledger.json"
LEDGER_FILENAME = CANONICAL_LEDGER_FILENAME


def _load_validator() -> Draft202012Validator:
    raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected schema root: {SCHEMA_PATH}")
    Draft202012Validator.check_schema(raw)
    return Draft202012Validator(raw)


_VALIDATOR = _load_validator()


def discover_ledger_files(root: Path) -> list[Path]:
    path = root / LEDGER_FILENAME
    return [path] if path.is_file() else []


def validate_ledger_file(path: Path) -> list[ValidationFinding]:
    raw = _load_yaml(path)
    if isinstance(raw, list):
        return raw
    findings = _validate_schema(path, raw)
    if findings:
        return findings
    try:
        document = PortfolioLedgerDocument.model_validate(raw)
        if path.name == CANONICAL_LEDGER_FILENAME and any(
            price.source_kind == "test_fixture" for price in document.market_prices
        ):
            raise PortfolioLedgerError("canonical portfolio ledger cannot use test_fixture prices")
        snapshot = reconcile_portfolio(document)
    except (ValueError, PortfolioLedgerError) as error:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="ledger.reconciliation",
                message=str(error),
            )
        ]
    for warning in snapshot.warnings:
        override = (
            f"; override={warning.override_id} until {warning.override_expires_at.isoformat()}"
            if warning.overridden and warning.override_expires_at is not None
            else "; human override required"
        )
        findings.append(
            ValidationFinding(
                severity="warning",
                target=path,
                code=warning.code,
                message=(
                    f"{warning.scope}={warning.key} is {warning.actual_pct:.2f}% "
                    f"against warning line {warning.warning_pct:.2f}%{override}"
                ),
                location=f"warnings.{warning.scope}.{warning.key}",
            )
        )
    return findings


def _load_yaml(path: Path) -> Mapping[str, object] | list[ValidationFinding]:
    try:
        raw = safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="ledger.io",
                message=f"failed to read ledger: {error}",
            )
        ]
    except yaml.YAMLError as error:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="ledger.invalid-yaml",
                message=f"YAML parse failed: {error}",
            )
        ]
    if not isinstance(raw, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="ledger.non-mapping",
                message="portfolio ledger root must be a mapping",
            )
        ]
    return raw


def _validate_schema(path: Path, document: Mapping[str, object]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for error in _VALIDATOR.iter_errors(document):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=f"ledger.{error.validator or 'invalid'}",
                message=str(error.message),
                location=_format_path(error.absolute_path),
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
