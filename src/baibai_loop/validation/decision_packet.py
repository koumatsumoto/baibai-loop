"""Validate canonical decision-packet schema and semantic calculations."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from baibai_loop.foundation.errors import ValidationFinding
from baibai_loop.foundation.yaml_io import safe_load
from baibai_loop.thesis.decision_packet import (
    DecisionPacketDocument,
    DecisionPacketError,
    evaluate_decision_packet,
    load_independent_review,
)

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "records" / "_schemas" / "decision-packet.json"


def _load_validator() -> Draft202012Validator:
    raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected schema root: {SCHEMA_PATH}")
    Draft202012Validator.check_schema(raw)
    return Draft202012Validator(raw, format_checker=FormatChecker())


_VALIDATOR = _load_validator()
_CANONICAL_NAME = re.compile(
    r"^(?P<as_of>\d{4}-\d{2}-\d{2})-(?P<ticker>[0-9A-Z]{4})-decision\.yaml$"
)


def discover_decision_packet_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*-decision.yaml") if path.is_file())


def validate_decision_packet_file(path: Path) -> list[ValidationFinding]:
    raw = _load_yaml(path)
    if isinstance(raw, list):
        return raw
    findings = _validate_schema(path, raw)
    if findings:
        return findings
    try:
        document = DecisionPacketDocument.model_validate(raw)
        findings.extend(_validate_canonical_identity(path, document))
        if findings:
            return findings
        review = None
        if document.independent_review_ref is not None:
            review_path = _review_path(path, document.independent_review_ref)
            review = load_independent_review(review_path)
        result = evaluate_decision_packet(document, review=review)
    except (ValueError, DecisionPacketError) as error:
        return [_finding(path, "error", "decision-packet.invalid", str(error))]
    findings.extend(
        _finding(path, "error", "decision-packet.incomplete", message) for message in result.errors
    )
    findings.extend(
        _finding(path, "warning", "decision-packet.warning", message) for message in result.warnings
    )
    return findings


def _validate_canonical_identity(
    path: Path, document: DecisionPacketDocument
) -> list[ValidationFinding]:
    match = _CANONICAL_NAME.fullmatch(path.name)
    if match is None:
        return [
            _finding(
                path,
                "error",
                "decision-packet.identity",
                "decision packet filename must use YYYY-MM-DD-<ticker>-decision.yaml",
            )
        ]
    findings: list[ValidationFinding] = []
    if match.group("ticker") != document.input_snapshot.ticker:
        findings.append(
            _finding(
                path,
                "error",
                "decision-packet.identity",
                "filename ticker does not match input_snapshot.ticker",
                location="input_snapshot.ticker",
            )
        )
    if match.group("as_of") != document.input_snapshot.as_of.isoformat():
        findings.append(
            _finding(
                path,
                "error",
                "decision-packet.identity",
                "filename date does not match input_snapshot.as_of",
                location="input_snapshot.as_of",
            )
        )
    expected_year, expected_month = match.group("as_of").split("-")[:2]
    if path.parent.name != expected_month or path.parent.parent.name != expected_year:
        findings.append(
            _finding(
                path,
                "error",
                "decision-packet.identity",
                "decision packet path year/month does not match filename date",
            )
        )
    return findings


def _review_path(packet_path: Path, review_ref: str) -> Path:
    root = packet_path.resolve().parent
    resolved = (root / review_ref).resolve()
    if not resolved.is_relative_to(root):
        raise DecisionPacketError("independent_review_ref must stay beside the packet")
    return resolved


def _load_yaml(path: Path) -> Mapping[str, object] | list[ValidationFinding]:
    try:
        raw = safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        return [_finding(path, "error", "decision-packet.io", str(error))]
    except yaml.YAMLError as error:
        return [_finding(path, "error", "decision-packet.invalid-yaml", str(error))]
    if not isinstance(raw, Mapping):
        return [
            _finding(
                path,
                "error",
                "decision-packet.non-mapping",
                "decision packet root must be a mapping",
            )
        ]
    return raw


def _validate_schema(path: Path, document: Mapping[str, object]) -> list[ValidationFinding]:
    return [
        _finding(
            path,
            "error",
            f"decision-packet.{error.validator or 'invalid'}",
            error.message,
            location=_format_path(error.absolute_path),
        )
        for error in _VALIDATOR.iter_errors(document)
    ]


def _finding(
    path: Path,
    severity: str,
    code: str,
    message: str,
    *,
    location: str | None = None,
) -> ValidationFinding:
    if severity not in {"error", "warning"}:
        raise ValueError(f"invalid finding severity: {severity}")
    return ValidationFinding(
        severity=severity,  # type: ignore[arg-type]
        target=path,
        code=code,
        message=message,
        location=location,
    )


def _format_path(parts: Iterable[Any]) -> str:
    rendered: list[str] = []
    for part in parts:
        rendered.append(
            f"[{part}]" if isinstance(part, int) else f".{part}" if rendered else str(part)
        )
    return "".join(rendered)
