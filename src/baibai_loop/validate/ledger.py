from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from baibai_loop.ledger.io import validate_decision_register_jsonl

from .errors import ValidationFinding

SCHEMA_ROOT = Path(__file__).resolve().parents[3] / "records" / "_schemas"


def discover_ledger_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in (root / "research-decisions").glob("*.jsonl") if path.is_file())


def validate_ledger_file(path: Path) -> list[ValidationFinding]:
    validator = _load_validator(SCHEMA_ROOT / "decision-register.json")
    findings: list[ValidationFinding] = []
    records: list[dict[str, Any]] = []
    root = _repo_root(path)
    candidate_document_cache: dict[str, dict[str, Any] | None] = {}
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
    parse_errors = False
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            parse_errors = True
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
            parse_errors = True
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
        records.append(record)
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
        findings.extend(_check_required_lineage_fields(path, line_number, record))
        findings.extend(
            _check_candidate_decision_coverage(
                path,
                line_number,
                record,
                root=root,
                candidate_document_cache=candidate_document_cache,
            )
        )
    if not parse_errors:
        for message in validate_decision_register_jsonl(path):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="ledger.decision-register",
                    message=message,
                )
            )
        findings.extend(_check_candidate_coverage_from_candidates(path, records))
    return findings


def _check_required_lineage_fields(
    path: Path, line_number: int, record: dict[str, Any]
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    if record.get("provenance") not in {"regenerated", "manual"}:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="ledger.provenance",
                message="decision register rows require provenance: regenerated | manual",
                location=f"line {line_number}.provenance",
            )
        )
    tracking = record.get("tracking")
    if not isinstance(tracking, dict) or tracking.get("mode") not in {
        "post_approval",
        "re_examination",
        "missed_opportunity",
        "none",
    }:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="ledger.tracking-mode",
                message="decision register rows require tracking.mode",
                location=f"line {line_number}.tracking.mode",
            )
        )
    return findings


def _check_candidate_decision_coverage(
    path: Path,
    line_number: int,
    record: dict[str, Any],
    *,
    root: Path,
    candidate_document_cache: dict[str, dict[str, Any] | None],
) -> list[ValidationFinding]:
    if record.get("candidate_decision") != "not_reviewed":
        return []
    candidate_ref = record.get("candidate_ref")
    if not isinstance(candidate_ref, dict):
        return []
    candidates_ref = candidate_ref.get("candidates_ref")
    ticker = candidate_ref.get("ticker")
    if not isinstance(candidates_ref, str) or not isinstance(ticker, str):
        return []
    document = _load_candidate_document(root, candidates_ref, candidate_document_cache)
    if document is None:
        return []
    if document.get("requires_decision_coverage") is not True:
        return []
    candidate = None
    candidates = document.get("candidates")
    if isinstance(candidates, list):
        for item in candidates:
            if isinstance(item, dict) and item.get("ticker") == ticker:
                candidate = item
                break
    if candidate is None:
        return []
    if candidate.get("playbook_screen_result") not in {"hit", "near_threshold"}:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="ledger.not-reviewed-coverage",
                message="not_reviewed rows are only allowed for hit or near-threshold candidates",
                location=f"line {line_number}.candidate_ref",
            )
        ]
    is_hard_excluded = (
        candidate.get("policy_gate_result") == "excluded"
        or candidate.get("liquidity_gate_result") == "excluded"
    )
    if is_hard_excluded:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="ledger.not-reviewed-hard-excluded",
                message="hard-excluded candidates must not be tracked as not_reviewed",
                location=f"line {line_number}.candidate_ref",
            )
        ]
    return []


def _load_candidate_document(
    root: Path,
    candidates_ref: str,
    cache: dict[str, dict[str, Any] | None],
) -> dict[str, Any] | None:
    if candidates_ref in cache:
        return cache[candidates_ref]
    candidate_path = root / candidates_ref
    try:
        document = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        cache[candidates_ref] = None
        return None
    if not isinstance(document, dict):
        cache[candidates_ref] = None
        return None
    cache[candidates_ref] = document
    return document


def _check_candidate_coverage_from_candidates(
    path: Path,
    records: list[dict[str, Any]],
) -> list[ValidationFinding]:
    root = _repo_root(path)
    candidates_root = root / "records/04-candidates"
    if not candidates_root.is_dir():
        return []
    covered = _covered_candidate_refs(records)
    findings: list[ValidationFinding] = []
    for candidate_path in sorted(candidates_root.rglob("*.yaml")):
        try:
            document = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(document, dict):
            continue
        requires = document.get("requires_decision_coverage")
        if requires is False:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="ledger.candidate-coverage-disabled",
                    message="candidate decision coverage must stay enabled for current records",
                    location=str(candidate_path.relative_to(root)),
                )
            )
            continue
        if requires is not True:
            continue
        candidates = document.get("candidates")
        if not isinstance(candidates, list):
            continue
        candidates_ref = str(candidate_path.relative_to(root))
        for candidate in candidates:
            if not isinstance(candidate, dict) or not _requires_candidate_decision(candidate):
                continue
            candidate_id = candidate.get("candidate_id")
            ticker = candidate.get("ticker")
            key = (
                candidates_ref,
                str(candidate_id) if isinstance(candidate_id, str) else "",
                str(ticker) if isinstance(ticker, str) else "",
            )
            if key not in covered:
                findings.append(
                    ValidationFinding(
                        severity="error",
                        target=path,
                        code="ledger.candidate-decision-coverage",
                        message=(
                            "post-policy hit or near-threshold candidates require a "
                            "candidate_screen or research_memo decision event"
                        ),
                        location=f"{candidates_ref}:{ticker}",
                    )
                )
    return findings


def _covered_candidate_refs(records: list[dict[str, Any]]) -> set[tuple[str, str, str]]:
    covered: set[tuple[str, str, str]] = set()
    for record in records:
        if record.get("decision_scope") not in {"candidate_screen", "research_memo"}:
            continue
        candidate_ref = record.get("candidate_ref")
        if not isinstance(candidate_ref, dict):
            continue
        candidates_ref = candidate_ref.get("candidates_ref")
        ticker = candidate_ref.get("ticker")
        candidate_id = candidate_ref.get("candidate_id")
        if isinstance(candidates_ref, str) and isinstance(ticker, str):
            covered.add(
                (
                    candidates_ref,
                    str(candidate_id) if isinstance(candidate_id, str) else "",
                    ticker,
                )
            )
    return covered


def _requires_candidate_decision(candidate: dict[str, Any]) -> bool:
    if candidate.get("playbook_screen_result") not in {"hit", "near_threshold"}:
        return False
    if candidate.get("policy_gate_result") == "excluded":
        return False
    if candidate.get("liquidity_gate_result") == "excluded":
        return False
    return candidate.get("macro_regime_gate_result") != "blocked"


def _repo_root(path: Path) -> Path:
    for parent in (path.parent, *path.parents):
        if (parent / "records").is_dir():
            return parent
    return path.parents[2]


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
