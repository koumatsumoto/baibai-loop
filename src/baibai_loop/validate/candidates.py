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

from .errors import ValidationFinding

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "records" / "_schemas" / "candidates.json"
_REMOVED_ROOT_FIELDS = frozenset(
    {
        "screening_rules_snapshot",
        "metric_catalog_snapshot",
        "policy_snapshot",
        "cache_manifest_hash",
    }
)


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
        document = yaml.safe_load(text)
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
    findings.extend(_check_removed_root_fields(path, document))
    findings.extend(_check_business_lineage(path, document))
    return findings


def _check_removed_root_fields(path: Path, document: Mapping[str, Any]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for field in sorted(_REMOVED_ROOT_FIELDS):
        if field in document:
            findings.append(
                _finding(
                    path,
                    "candidates.removed-root-field",
                    f"{field} is no longer part of candidates output",
                    field,
                )
            )
    return findings


def _check_business_lineage(
    path: Path,
    document: Mapping[str, Any],
) -> list[ValidationFinding]:
    run_id = document.get("run_id")
    asof_date = document.get("asof_date")
    candidates = document.get("candidates")
    if not isinstance(run_id, str) or not isinstance(candidates, list):
        return []
    findings: list[ValidationFinding] = []
    seen_candidate_ids: set[str] = set()
    seen_candidate_keys: set[str] = set()
    seen_evidence_hit_ids: set[str] = set()
    for candidate_index, candidate in enumerate(candidates):
        if not isinstance(candidate, Mapping):
            continue
        ticker = candidate.get("ticker")
        if isinstance(candidate.get("screen_run_id"), str) and candidate["screen_run_id"] != run_id:
            findings.append(
                _finding(
                    path,
                    "candidates.screen-run-id",
                    "candidate.screen_run_id must equal root run_id",
                    f"candidates[{candidate_index}].screen_run_id",
                )
            )
        expected_key = f"{run_id}:{ticker}" if isinstance(ticker, str) else None
        if (
            isinstance(candidate.get("candidate_key"), str)
            and candidate["candidate_key"] != expected_key
        ):
            findings.append(
                _finding(
                    path,
                    "candidates.candidate-key",
                    "candidate_key must equal <run_id>:<ticker>",
                    f"candidates[{candidate_index}].candidate_key",
                )
            )
        candidate_id = candidate.get("candidate_id")
        expected_candidate_id = (
            f"candidate-{asof_date}-{ticker}"
            if isinstance(asof_date, str) and isinstance(ticker, str)
            else None
        )
        if isinstance(candidate_id, str):
            if candidate_id != expected_candidate_id:
                findings.append(
                    _finding(
                        path,
                        "candidates.candidate-id",
                        "candidate_id must equal candidate-<asof_date>-<ticker>",
                        f"candidates[{candidate_index}].candidate_id",
                    )
                )
            if candidate_id in seen_candidate_ids:
                findings.append(
                    _finding(
                        path,
                        "candidates.duplicate-candidate-id",
                        "candidate_id must be unique within the screen run",
                        f"candidates[{candidate_index}].candidate_id",
                    )
                )
            seen_candidate_ids.add(candidate_id)
        candidate_key = candidate.get("candidate_key")
        if isinstance(candidate_key, str):
            if candidate_key in seen_candidate_keys:
                findings.append(
                    _finding(
                        path,
                        "candidates.duplicate-candidate-key",
                        "candidate_key must be unique within the screen run",
                        f"candidates[{candidate_index}].candidate_key",
                    )
                )
            seen_candidate_keys.add(candidate_key)
        hits = candidate.get("evidence_hits")
        if not isinstance(hits, list):
            continue
        for hit_index, hit in enumerate(hits):
            if not isinstance(hit, Mapping):
                continue
            hit_id = hit.get("evidence_hit_id")
            if isinstance(hit_id, str):
                if hit_id in seen_evidence_hit_ids:
                    findings.append(
                        _finding(
                            path,
                            "candidates.duplicate-evidence-hit-id",
                            "evidence_hit_id must be unique within the screen run",
                            f"candidates[{candidate_index}].evidence_hits[{hit_index}].evidence_hit_id",
                        )
                    )
                seen_evidence_hit_ids.add(hit_id)
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
