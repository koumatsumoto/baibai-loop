"""Validate candidates YAML artefacts against the central jsonschema.

candidates は YAML 正本である。R6 のコア schema として
`records/_schemas/candidates.json` で構造を中央集約し、本モジュールはその schema
で artefact を検証する。playbook 別 schema (本文 section 構造) は本
モジュールの対象外。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from baibai_loop.foundation.errors import ValidationFinding
from baibai_loop.foundation.yaml_io import safe_load

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "records" / "_schemas" / "candidates.json"


def _load_validator() -> Draft202012Validator:
    raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected schema root: {SCHEMA_PATH}")
    Draft202012Validator.check_schema(raw)
    return Draft202012Validator(raw)


_VALIDATOR = _load_validator()


def validate_candidates_file(path: Path) -> list[ValidationFinding]:
    """Validate a single candidates YAML file and return all findings."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="candidates.io",
                message=f"failed to read file: {exc}",
            )
        ]
    try:
        document = safe_load(text)
    except yaml.YAMLError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="candidates.invalid-yaml",
                message=f"YAML parse failed: {exc}",
            )
        ]
    if not isinstance(document, dict):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="candidates.non-mapping",
                message="candidates YAML root must be a mapping",
            )
        ]
    findings: list[ValidationFinding] = []
    for error in _VALIDATOR.iter_errors(document):
        validator_keyword = error.validator or "invalid"
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=f"candidates.{validator_keyword}",
                message=str(error.message),
                location=_format_path(error.absolute_path),
            )
        )
    findings.extend(_check_business_lineage(path, document))
    return findings


def _check_business_lineage(
    path: Path,
    document: Mapping[str, Any],
) -> list[ValidationFinding]:
    run_id = document.get("run_id")
    candidates = document.get("candidates")
    if not isinstance(run_id, str) or not isinstance(candidates, list):
        return []
    findings: list[ValidationFinding] = []
    seen_tickers: set[str] = set()
    for candidate_index, candidate in enumerate(candidates):
        if not isinstance(candidate, Mapping):
            continue
        ticker = candidate.get("ticker")
        if isinstance(ticker, str):
            if ticker in seen_tickers:
                findings.append(
                    _finding(
                        path,
                        "candidates.duplicate-ticker",
                        "candidate ticker must be unique within the screen run",
                        f"candidates[{candidate_index}].ticker",
                    )
                )
            seen_tickers.add(ticker)
        hits = candidate.get("evidence_hits")
        if not isinstance(hits, list):
            continue
        for hit_index, hit in enumerate(hits):
            if not isinstance(hit, Mapping):
                continue
            source_status = hit.get("source_status")
            if source_status != "ok" and hit.get("sizing_eligible") is True:
                findings.append(
                    _finding(
                        path,
                        "candidates.ineligible-source-status",
                        "non-ok evidence source_status must not be sizing_eligible",
                        f"candidates[{candidate_index}].evidence_hits[{hit_index}].sizing_eligible",
                    )
                )
    return findings


def discover_candidates_files(root: Path) -> list[Path]:
    """Return all records/04-candidates/*.yaml files under ``root`` in sorted order."""
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.yaml") if p.is_file())


def _format_path(parts: Iterable[object]) -> str:
    parts_list = list(parts)
    if not parts_list:
        return ""
    rendered: list[str] = []
    for part in parts_list:
        if isinstance(part, int):
            rendered.append(f"[{part}]")
        else:
            rendered.append(f".{part}" if rendered else str(part))
    return "".join(rendered)


def _finding(path: Path, code: str, message: str, location: str) -> ValidationFinding:
    return ValidationFinding(
        severity="error",
        target=path,
        code=code,
        message=message,
        location=location,
    )
