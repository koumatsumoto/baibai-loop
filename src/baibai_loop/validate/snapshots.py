"""Validate immutable snapshot references and migration manifests."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import yaml

from .errors import ValidationFinding

_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)
_SHA_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_SNAPSHOT_ROOTS: tuple[Path, ...] = (
    Path("records/01-policy/2026"),
    Path("records/_approval-rules"),
    Path("records/_calendars/business-days"),
    Path("records/_calendars/corporate-actions"),
    Path("records/_calendars/events"),
    Path("records/_config/exposure-buckets"),
    Path("records/_config/metric-catalog"),
    Path("records/_config/screening-rules"),
    Path("records/_config/sector-baselines"),
    Path("records/_market-data/2026"),
    Path("records/_playbooks"),
    Path("records/_portfolio-exposure/2026"),
    Path("records/_universe-snapshots/2026"),
)

_REFERENCE_ROOTS: tuple[Path, ...] = (
    Path("records/_benchmarks"),
    Path("records/04-candidates"),
    Path("records/05-research"),
    Path("records/06-trades"),
    Path("records/07-reviews"),
    Path("records/_ledger"),
    Path("records/_migrations"),
)


def validate_snapshot_integrity(root: Path) -> list[ValidationFinding]:
    """Validate snapshot hash contracts across records."""
    findings: list[ValidationFinding] = []
    findings.extend(_validate_changelogs(root))
    findings.extend(_validate_snapshot_payloads(root))
    findings.extend(_validate_snapshot_references(root))
    findings.extend(_validate_migration_digests(root))
    return findings


def discover_snapshot_validation_files(root: Path) -> list[Path]:
    """Return files that participate in snapshot validation for CLI counts."""
    files: list[Path] = []
    records_root = root / "records"
    if not records_root.exists():
        return files
    files.extend(sorted(records_root.rglob("_changelog.jsonl")))
    for rel_root in (*_SNAPSHOT_ROOTS, *_REFERENCE_ROOTS):
        base = root / rel_root
        if not base.exists():
            continue
        files.extend(
            path
            for path in sorted(base.rglob("*"))
            if path.is_file() and path.suffix in {".yaml", ".yml", ".md", ".jsonl"}
        )
    files.extend(sorted((root / "records" / "_migrations").glob("*.sha256")))
    return sorted(set(files))


def _validate_changelogs(root: Path) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for path in sorted((root / "records").rglob("_changelog.jsonl")):
        for line_no, row, error in _iter_jsonl(path):
            if error is not None:
                findings.append(error)
                continue
            if row is None:
                continue
            findings.extend(_check_snapshot_ref(root, path, row, location=f"line {line_no}"))
    return findings


def _validate_snapshot_payloads(root: Path) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for rel_root in _SNAPSHOT_ROOTS:
        base = root / rel_root
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.name.startswith("_"):
                continue
            parsed = _load_structured_payload(path)
            if isinstance(parsed, ValidationFinding):
                findings.append(parsed)
                continue
            if isinstance(parsed, Mapping) and "content_sha256" in parsed:
                findings.append(
                    ValidationFinding(
                        severity="error",
                        target=path,
                        code="snapshot.self-hash",
                        message="snapshot payload must not contain its own content_sha256",
                        location="content_sha256",
                    )
                )
    return findings


def _validate_snapshot_references(root: Path) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for rel_root in _REFERENCE_ROOTS:
        base = root / rel_root
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in {".yaml", ".yml", ".md", ".jsonl"}:
                continue
            if path.suffix == ".jsonl":
                for line_no, row, error in _iter_jsonl(path):
                    if error is not None:
                        findings.append(error)
                        continue
                    findings.extend(_check_nested_refs(root, path, row, prefix=f"line {line_no}"))
                continue
            parsed = _load_structured_payload(path)
            if isinstance(parsed, ValidationFinding):
                findings.append(parsed)
                continue
            findings.extend(_check_nested_refs(root, path, parsed, prefix=None))
    return findings


def _validate_migration_digests(root: Path) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    migrations_root = root / "records" / "_migrations"
    if not migrations_root.exists():
        return findings
    payload_hash = _records_payload_hash(root)
    for manifest in sorted(migrations_root.glob("*.jsonl")):
        sidecar = manifest.with_suffix(manifest.suffix + ".sha256")
        if not sidecar.exists():
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=manifest,
                    code="migration.digest-missing",
                    message="migration manifest must have a .sha256 sidecar",
                )
            )
            continue
        expected = sidecar.read_text(encoding="utf-8").strip().split()[0]
        actual = _raw_sha256(manifest)
        if expected != actual:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=sidecar,
                    code="migration.digest-mismatch",
                    message=f"sidecar digest {expected!r} does not match manifest {actual!r}",
                )
            )
        for line_no, row, error in _iter_jsonl(manifest):
            if error is not None:
                findings.append(error)
                continue
            if row is None:
                continue
            expected_payload = row.get("target_payload_tree_sha")
            if expected_payload is None:
                continue
            if not isinstance(expected_payload, str) or not _SHA_RE.match(expected_payload):
                findings.append(
                    ValidationFinding(
                        severity="error",
                        target=manifest,
                        code="migration.payload-hash-format",
                        message="target_payload_tree_sha must be sha256:<64-hex>",
                        location=f"line {line_no}.target_payload_tree_sha",
                    )
                )
            elif expected_payload != payload_hash:
                findings.append(
                    ValidationFinding(
                        severity="error",
                        target=manifest,
                        code="migration.payload-mismatch",
                        message=(f"target payload hash is {payload_hash}, not {expected_payload}"),
                        location=f"line {line_no}.target_payload_tree_sha",
                    )
                )
    return findings


def _check_nested_refs(
    root: Path,
    target: Path,
    value: Any,
    *,
    prefix: str | None,
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for location, node in _walk_mappings(value, prefix=prefix):
        findings.extend(_check_snapshot_ref(root, target, node, location=location))
    return findings


def _check_snapshot_ref(
    root: Path,
    target: Path,
    node: Mapping[str, object],
    *,
    location: str,
) -> list[ValidationFinding]:
    ref = node.get("ref_path") or node.get("snapshot_path") or node.get("ref")
    digest = node.get("content_sha256")
    if ref is None and digest is None:
        return []
    findings: list[ValidationFinding] = []
    if not isinstance(ref, str) or not ref:
        findings.append(
            ValidationFinding(
                severity="error",
                target=target,
                code="snapshot.ref-missing",
                message="snapshot reference must include ref_path, snapshot_path, or ref",
                location=location,
            )
        )
        return findings
    if not isinstance(digest, str) or not _SHA_RE.match(digest):
        findings.append(
            ValidationFinding(
                severity="error",
                target=target,
                code="snapshot.hash-format",
                message="snapshot reference must include content_sha256: sha256:<64-hex>",
                location=location,
            )
        )
        return findings
    ref_path = root / ref
    if not ref_path.is_file():
        findings.append(
            ValidationFinding(
                severity="error",
                target=target,
                code="snapshot.ref-not-found",
                message=f"referenced snapshot does not exist: {ref}",
                location=location,
            )
        )
        return findings
    actual = f"sha256:{_raw_sha256(ref_path)}"
    if actual != digest:
        findings.append(
            ValidationFinding(
                severity="error",
                target=target,
                code="snapshot.hash-mismatch",
                message=f"{ref} hash is {actual}, not {digest}",
                location=location,
            )
        )
    return findings


def _load_structured_payload(path: Path) -> object | ValidationFinding:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return ValidationFinding(
            severity="error",
            target=path,
            code="snapshot.io",
            message=f"failed to read file: {exc}",
        )
    if path.suffix == ".md":
        match = _FRONT_MATTER_RE.match(text)
        if not match:
            return {}
        text = match.group(1)
    try:
        loaded: object = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return ValidationFinding(
            severity="error",
            target=path,
            code="snapshot.invalid-yaml",
            message=f"YAML parse failed: {exc}",
        )
    return loaded


def _iter_jsonl(
    path: Path,
) -> Iterator[tuple[int, Mapping[str, object] | None, ValidationFinding | None]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        yield (
            0,
            None,
            ValidationFinding(
                severity="error",
                target=path,
                code="snapshot.io",
                message=f"failed to read file: {exc}",
            ),
        )
        return
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            yield (
                line_no,
                None,
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="snapshot.invalid-jsonl",
                    message=f"JSONL parse failed at line {line_no}: {exc}",
                    location=f"line {line_no}",
                ),
            )
            continue
        if not isinstance(row, Mapping):
            yield (
                line_no,
                None,
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="snapshot.jsonl-non-mapping",
                    message=f"JSONL line {line_no} must be an object",
                    location=f"line {line_no}",
                ),
            )
            continue
        yield line_no, row, None


def _walk_mappings(value: Any, *, prefix: str | None) -> Iterator[tuple[str, Mapping[str, object]]]:
    if isinstance(value, Mapping):
        location = prefix or "<root>"
        yield location, value
        for key, child in value.items():
            child_prefix = f"{location}.{key}" if prefix else str(key)
            yield from _walk_mappings(child, prefix=child_prefix)
    elif isinstance(value, list):
        location = prefix or "<root>"
        for index, child in enumerate(value):
            yield from _walk_mappings(child, prefix=f"{location}[{index}]")


def _raw_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _records_payload_hash(root: Path) -> str:
    records_root = root / "records"
    rows: list[str] = []
    if not records_root.exists():
        return f"sha256:{hashlib.sha256(b'').hexdigest()}"
    for path in sorted(records_root.rglob("*")):
        if not path.is_file() or "_migrations" in path.relative_to(records_root).parts:
            continue
        rows.append(f"{_raw_sha256(path)}  {path.relative_to(root).as_posix()}\n")
    return f"sha256:{hashlib.sha256(''.join(rows).encode('utf-8')).hexdigest()}"
