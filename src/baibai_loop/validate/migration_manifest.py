"""Validate destructive domain-model migration manifests."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .errors import ValidationFinding

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_OLD_PATH_PREFIXES = (
    "records/_ledger/paper/",
    "records/_ledger/skipped/",
    "records/03-candidates/",
    "records/04-research/",
    "records/05-trades/",
    "records/06-reviews/",
)


def discover_migration_manifest_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.glob("*.jsonl") if path.is_file())


def validate_migration_manifest_file(path: Path) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    rows: list[Mapping[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="migration-manifest.io",
                message=f"failed to read migration manifest: {exc}",
            )
        ]
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            findings.append(_finding(path, "migration-manifest.invalid-json", str(exc), line_no))
            continue
        if not isinstance(row, Mapping):
            findings.append(
                _finding(path, "migration-manifest.non-object", "row must be an object", line_no)
            )
            continue
        rows.append(row)
        findings.extend(_check_row(path, row, line_no))
    if not any(row.get("granularity") == "row" for row in rows):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="migration-manifest.row-granularity",
                message="manifest must include row-level migration audit rows",
            )
        )
    if not any("unregistered_candidate_population" in row for row in rows):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code="migration-manifest.unregistered-summary",
                message="manifest must include unregistered_candidate_population summary",
            )
        )
    return findings


def _check_row(path: Path, row: Mapping[str, Any], line_no: int) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    required = ("migration_run_id", "migration_strategy", "granularity")
    for field in required:
        if field not in row:
            findings.append(
                _finding(
                    path,
                    "migration-manifest.required",
                    f"missing required field: {field}",
                    line_no,
                    field,
                )
            )
    if row.get("granularity") == "row":
        row_required = (
            "old_path",
            "old_row_raw",
            "old_row_sha256",
            "old_line_number",
            "new_path",
            "new_row_sha256",
            "new_line_number",
            "new_decision_id",
            "consolidation_group_id",
            "schema_from",
            "schema_to",
            "migration_tool",
        )
        for field in row_required:
            if field not in row:
                findings.append(
                    _finding(
                        path,
                        "migration-manifest.row-required",
                        f"missing row-level audit field: {field}",
                        line_no,
                        field,
                    )
                )
        findings.extend(_check_row_hash_fields(path, row, line_no))
        findings.extend(_check_old_row_payload(path, row, line_no))
        findings.extend(_check_old_row_reference(path, row, line_no))
        findings.extend(_check_new_row_reference(path, row, line_no))
    return findings


def _check_old_row_reference(
    path: Path,
    row: Mapping[str, Any],
    line_no: int,
) -> list[ValidationFinding]:
    old_path = row.get("old_path")
    line_number = row.get("old_line_number")
    digest = row.get("old_row_sha256")
    old_row_raw = row.get("old_row_raw")
    type_findings: list[ValidationFinding] = []
    expected_types = {
        "old_path": (old_path, str),
        "old_line_number": (line_number, int),
        "old_row_sha256": (digest, str),
        "old_row_raw": (old_row_raw, str),
    }
    for field, (value, expected_type) in expected_types.items():
        if not isinstance(value, expected_type):
            type_findings.append(
                _finding(
                    path,
                    "migration-manifest.old-row-reference-type",
                    f"{field} must have the correct type for old row source lookup",
                    line_no,
                    field,
                )
            )
    if type_findings:
        return type_findings
    assert isinstance(old_path, str)
    assert isinstance(line_number, int)
    assert isinstance(digest, str)
    assert isinstance(old_row_raw, str)
    if not old_path.startswith(_OLD_PATH_PREFIXES):
        return [
            _finding(
                path,
                "migration-manifest.old-row-reference-path",
                "old_path must point at an allowed pre-migration record root",
                line_no,
                "old_path",
            )
        ]
    raw_line = _old_row_from_reference(_repo_root(path), old_path, line_number)
    if raw_line is None:
        return [
            _finding(
                path,
                "migration-manifest.old-row-reference-missing",
                "old_path and old_line_number must resolve to an auditable source row",
                line_no,
                "old_path",
            )
        ]
    actual_digest = "sha256:" + hashlib.sha256(raw_line.encode("utf-8")).hexdigest()
    findings: list[ValidationFinding] = []
    if digest != actual_digest:
        findings.append(
            _finding(
                path,
                "migration-manifest.old-row-reference-hash",
                "old_row_sha256 must match old_path/old_line_number source row",
                line_no,
                "old_row_sha256",
            )
        )
    if old_row_raw != raw_line:
        findings.append(
            _finding(
                path,
                "migration-manifest.old-row-reference-raw",
                "old_row_raw must match old_path/old_line_number source row",
                line_no,
                "old_row_raw",
            )
        )
    return findings


def _old_row_from_reference(root: Path, old_path: str, line_number: int) -> str | None:
    text: str | None = None
    if (root / ".git").exists():
        for ref in ("origin/main", "HEAD^"):
            try:
                text = subprocess.check_output(
                    ["git", "show", f"{ref}:{old_path}"],
                    cwd=root,
                    text=True,
                    stderr=subprocess.DEVNULL,
                )
                break
            except (OSError, subprocess.CalledProcessError):
                text = None
    else:
        worktree_path = root / old_path
        if worktree_path.is_file():
            try:
                text = worktree_path.read_text(encoding="utf-8")
            except OSError:
                text = None
    if text is None:
        return None
    lines = text.splitlines()
    if line_number < 1 or line_number > len(lines):
        return None
    return lines[line_number - 1]


def _check_row_hash_fields(
    path: Path,
    row: Mapping[str, Any],
    line_no: int,
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for field in ("old_row_sha256", "new_row_sha256"):
        value = row.get(field)
        if not isinstance(value, str) or not _SHA256_RE.match(value):
            findings.append(
                _finding(
                    path,
                    "migration-manifest.sha256-format",
                    f"{field} must be sha256:<64 lowercase hex chars>",
                    line_no,
                    field,
                )
            )
    return findings


def _check_old_row_payload(
    path: Path,
    row: Mapping[str, Any],
    line_no: int,
) -> list[ValidationFinding]:
    old_row_raw = row.get("old_row_raw")
    digest = row.get("old_row_sha256")
    old_ledger_id = row.get("old_ledger_id")
    if not isinstance(old_row_raw, str) or not isinstance(digest, str):
        return []
    actual_digest = "sha256:" + hashlib.sha256(old_row_raw.encode("utf-8")).hexdigest()
    findings: list[ValidationFinding] = []
    if actual_digest != digest:
        findings.append(
            _finding(
                path,
                "migration-manifest.old-row-hash",
                "old_row_sha256 must match old_row_raw",
                line_no,
                "old_row_sha256",
            )
        )
    try:
        payload = json.loads(old_row_raw)
    except json.JSONDecodeError as exc:
        findings.append(
            _finding(
                path,
                "migration-manifest.old-row-json",
                f"old_row_raw is invalid JSON: {exc}",
                line_no,
                "old_row_raw",
            )
        )
        return findings
    if isinstance(old_ledger_id, str) and (
        not isinstance(payload, Mapping) or payload.get("ledger_id") != old_ledger_id
    ):
        findings.append(
            _finding(
                path,
                "migration-manifest.old-ledger-id",
                "old_ledger_id must match old_row_raw.ledger_id",
                line_no,
                "old_ledger_id",
            )
        )
    return findings


def _check_new_row_reference(
    path: Path,
    row: Mapping[str, Any],
    line_no: int,
) -> list[ValidationFinding]:
    new_path = row.get("new_path")
    line_number = row.get("new_line_number")
    digest = row.get("new_row_sha256")
    decision_id = row.get("new_decision_id")
    if (
        not isinstance(new_path, str)
        or not isinstance(line_number, int)
        or not isinstance(digest, str)
        or not isinstance(decision_id, str)
    ):
        return []
    repo_root = _repo_root(path)
    target = repo_root / new_path
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [
            _finding(
                path,
                "migration-manifest.new-row-missing",
                f"failed to read new_path: {exc}",
                line_no,
                "new_path",
            )
        ]
    if line_number < 1 or line_number > len(lines):
        return [
            _finding(
                path,
                "migration-manifest.new-line-number",
                "new_line_number must point at an existing JSONL row",
                line_no,
                "new_line_number",
            )
        ]
    raw_line = lines[line_number - 1]
    actual_digest = "sha256:" + hashlib.sha256(raw_line.encode("utf-8")).hexdigest()
    findings: list[ValidationFinding] = []
    if digest != actual_digest:
        findings.append(
            _finding(
                path,
                "migration-manifest.new-row-hash",
                "new_row_sha256 must match the referenced JSONL row",
                line_no,
                "new_row_sha256",
            )
        )
    try:
        payload = json.loads(raw_line)
    except json.JSONDecodeError as exc:
        findings.append(
            _finding(
                path,
                "migration-manifest.new-row-json",
                f"referenced JSONL row is invalid: {exc}",
                line_no,
                "new_line_number",
            )
        )
    else:
        if not isinstance(payload, Mapping) or payload.get("decision_event_id") != decision_id:
            findings.append(
                _finding(
                    path,
                    "migration-manifest.new-decision-id",
                    "new_decision_id must match the referenced JSONL row",
                    line_no,
                    "new_decision_id",
                )
            )
    return findings


def _repo_root(path: Path) -> Path:
    for parent in (path.parent, *path.parents):
        if (parent / "records").is_dir() and (parent / "src").is_dir():
            return parent
    return Path.cwd()


def _finding(
    path: Path,
    code: str,
    message: str,
    line_no: int | None = None,
    field: str | None = None,
) -> ValidationFinding:
    location = f"line {line_no}" if line_no else None
    if location and field:
        location = f"{location}.{field}"
    return ValidationFinding(
        severity="error",
        target=path,
        code=code,
        message=message,
        location=location,
    )
