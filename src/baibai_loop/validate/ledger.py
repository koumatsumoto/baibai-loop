from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from baibai_loop.ledger.io import validate_decision_register_jsonl

from .domain import repository_ref_error, resolve_repository_ref
from .errors import ValidationFinding

SCHEMA_ROOT = Path(__file__).resolve().parents[3] / "records" / "_schemas"
_REMOVED_HASH_FIELDS = frozenset({"content_" + "sha256", "row_" + "sha256"})
_REMOVED_REFERENCE_FIELDS = frozenset(
    {
        "playbook_snapshot",
        "_".join(("policy", "snapshot")),
        "portfolio_exposure_snapshot_ref",
        "calendars_snapshot",
        "universe_snapshot_ref",
        "input_snapshots",
        "screening_rules_snapshot",
        "metric_catalog_snapshot",
        "cache_manifest_hash",
        "snapshot_path",
        "latest_snapshot",
    }
)


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
        findings.extend(_check_removed_reference_fields(path, line_number, record))
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
            _check_candidate_ref_lineage(
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
    return findings


def _check_removed_reference_fields(
    path: Path, line_number: int, value: object, *, prefix: str = ""
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    if isinstance(value, dict):
        for key, child in value.items():
            location = f"{prefix}.{key}" if prefix else str(key)
            if key in _REMOVED_HASH_FIELDS:
                findings.append(
                    ValidationFinding(
                        severity="error",
                        target=path,
                        code="ledger.removed-hash-field",
                        message=f"{key} is no longer allowed in repository references",
                        location=f"line {line_number}.{location}",
                    )
                )
            if key in _REMOVED_REFERENCE_FIELDS:
                findings.append(
                    ValidationFinding(
                        severity="error",
                        target=path,
                        code="ledger.removed-reference-field",
                        message=f"{key} has been replaced by repository reference fields",
                        location=f"line {line_number}.{location}",
                    )
                )
            findings.extend(
                _check_removed_reference_fields(path, line_number, child, prefix=location)
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            location = f"{prefix}[{index}]" if prefix else f"[{index}]"
            findings.extend(
                _check_removed_reference_fields(path, line_number, child, prefix=location)
            )
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
    return []


def _check_candidate_ref_lineage(
    path: Path,
    line_number: int,
    record: dict[str, Any],
    *,
    root: Path,
    candidate_document_cache: dict[str, dict[str, Any] | None],
) -> list[ValidationFinding]:
    if not isinstance(record.get("candidate_ref"), dict):
        return []
    findings = _candidate_ref_document_findings(
        path,
        line_number,
        record,
        root=root,
        candidate_document_cache=candidate_document_cache,
    )
    if findings:
        return findings
    loaded = _candidate_ref_document_and_row(
        record,
        root=root,
        candidate_document_cache=candidate_document_cache,
    )
    if loaded is None:
        return []
    document, candidate = loaded
    candidate_ref = record["candidate_ref"]
    if not isinstance(candidate_ref, dict):
        return []

    findings = []
    ref_ticker = candidate_ref.get("ticker")
    if not isinstance(ref_ticker, str) or not ref_ticker:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="ledger.candidate-ref-ticker",
                message="candidate_ref.ticker is required",
                location=f"line {line_number}.candidate_ref.ticker",
            )
        )
    elif ref_ticker != record.get("ticker"):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="ledger.candidate-ref-ticker",
                message="candidate_ref.ticker must match ledger row ticker",
                location=f"line {line_number}.candidate_ref.ticker",
            )
        )
    ref_screen_run_id = candidate_ref.get("screen_run_id")
    document_run_id = document.get("run_id")
    if not isinstance(ref_screen_run_id, str) or not ref_screen_run_id:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="ledger.candidate-ref-screen-run-id",
                message="candidate_ref.screen_run_id is required",
                location=f"line {line_number}.candidate_ref.screen_run_id",
            )
        )
    elif isinstance(document_run_id, str) and ref_screen_run_id != document_run_id:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="ledger.candidate-ref-screen-run-id",
                message="candidate_ref.screen_run_id must match candidate document run_id",
                location=f"line {line_number}.candidate_ref.screen_run_id",
            )
        )

    candidate_id = candidate_ref.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="ledger.candidate-ref-candidate-id",
                message="candidate_ref.candidate_id is required",
                location=f"line {line_number}.candidate_ref.candidate_id",
            )
        )
    elif candidate is not None and candidate.get("candidate_id") != candidate_id:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="ledger.candidate-ref-candidate-id",
                message="candidate_ref.candidate_id must match candidate row candidate_id",
                location=f"line {line_number}.candidate_ref.candidate_id",
            )
        )
    elif candidate is None:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="ledger.candidate-ref-match",
                message=(
                    "candidate_ref must match a candidate row by ticker, "
                    "candidate_id, and screen_run_id"
                ),
                location=f"line {line_number}.candidate_ref",
            )
        )

    candidate_screen_run_id = candidate.get("screen_run_id") if candidate is not None else None
    if (
        isinstance(ref_screen_run_id, str)
        and candidate is not None
        and isinstance(candidate_screen_run_id, str)
        and ref_screen_run_id != candidate_screen_run_id
    ):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="ledger.candidate-ref-screen-run-id",
                message="candidate_ref.screen_run_id must match candidate row screen_run_id",
                location=f"line {line_number}.candidate_ref.screen_run_id",
            )
        )
    return findings


def _candidate_ref_document_findings(
    path: Path,
    line_number: int,
    record: dict[str, Any],
    *,
    root: Path,
    candidate_document_cache: dict[str, dict[str, Any] | None],
) -> list[ValidationFinding]:
    candidate_ref = record.get("candidate_ref")
    if not isinstance(candidate_ref, dict):
        return []
    candidates_ref = candidate_ref.get("candidates_ref")
    if not isinstance(candidates_ref, str) or not candidates_ref:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="ledger.candidate-ref-required",
                message="candidate_ref.candidates_ref is required",
                location=f"line {line_number}.candidate_ref.candidates_ref",
            )
        ]
    if candidates_ref in candidate_document_cache:
        return (
            []
            if candidate_document_cache[candidates_ref] is not None
            else [
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="ledger.candidate-ref-parse",
                    message="candidate_ref.candidates_ref failed to load previously",
                    location=f"line {line_number}.candidate_ref.candidates_ref",
                )
            ]
        )
    ref_error = repository_ref_error(candidates_ref, root=root)
    if ref_error is not None:
        candidate_document_cache[candidates_ref] = None
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="ledger.candidate-ref-path",
                message=ref_error,
                location=f"line {line_number}.candidate_ref.candidates_ref",
            )
        ]
    is_candidate_yaml = candidates_ref.startswith("records/04-candidates/") and Path(
        candidates_ref
    ).suffix in {".yaml", ".yml"}
    if not is_candidate_yaml:
        candidate_document_cache[candidates_ref] = None
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="ledger.candidate-ref-target",
                message=(
                    "candidate_ref.candidates_ref must point under "
                    "records/04-candidates/ and use YAML"
                ),
                location=f"line {line_number}.candidate_ref.candidates_ref",
            )
        ]
    candidate_path = resolve_repository_ref(root, candidates_ref)
    try:
        raw = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        candidate_document_cache[candidates_ref] = None
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="ledger.candidate-ref-missing",
                message=f"candidate_ref.candidates_ref does not exist: {candidates_ref}",
                location=f"line {line_number}.candidate_ref.candidates_ref",
            )
        ]
    except OSError as exc:
        candidate_document_cache[candidates_ref] = None
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="ledger.candidate-ref-parse",
                message=f"failed to read candidate_ref.candidates_ref: {exc}",
                location=f"line {line_number}.candidate_ref.candidates_ref",
            )
        ]
    except yaml.YAMLError as exc:
        candidate_document_cache[candidates_ref] = None
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="ledger.candidate-ref-parse",
                message=f"failed to parse candidate_ref.candidates_ref: {exc}",
                location=f"line {line_number}.candidate_ref.candidates_ref",
            )
        ]
    if not isinstance(raw, dict):
        candidate_document_cache[candidates_ref] = None
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="ledger.candidate-ref-parse",
                message="candidate_ref.candidates_ref must point to a candidates mapping",
                location=f"line {line_number}.candidate_ref.candidates_ref",
            )
        ]
    candidate_document_cache[candidates_ref] = raw
    return []


def _candidate_ref_document_and_row(
    record: dict[str, Any],
    *,
    root: Path,
    candidate_document_cache: dict[str, dict[str, Any] | None],
) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    candidate_ref = record.get("candidate_ref")
    if not isinstance(candidate_ref, dict):
        return None
    candidates_ref = candidate_ref.get("candidates_ref")
    ticker = candidate_ref.get("ticker")
    if not isinstance(candidates_ref, str) or not isinstance(ticker, str):
        return None
    document = _load_candidate_document(root, candidates_ref, candidate_document_cache)
    if document is None:
        return None
    candidate = None
    candidates = document.get("candidates")
    if isinstance(candidates, list):
        for item in candidates:
            if (
                isinstance(item, dict)
                and item.get("ticker") == ticker
                and item.get("candidate_id") == candidate_ref.get("candidate_id")
                and item.get("screen_run_id") == candidate_ref.get("screen_run_id")
            ):
                candidate = item
                break
    return document, candidate


def _load_candidate_document(
    root: Path,
    candidates_ref: str,
    cache: dict[str, dict[str, Any] | None],
) -> dict[str, Any] | None:
    if candidates_ref in cache:
        return cache[candidates_ref]
    if repository_ref_error(candidates_ref, root=root) is not None:
        cache[candidates_ref] = None
        return None
    is_candidate_yaml = candidates_ref.startswith("records/04-candidates/") and Path(
        candidates_ref
    ).suffix in {".yaml", ".yml"}
    if not is_candidate_yaml:
        cache[candidates_ref] = None
        return None
    candidate_path = resolve_repository_ref(root, candidates_ref)
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
