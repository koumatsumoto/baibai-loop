"""Validate repository reference links.

The CLI target remains named ``snapshots`` for operator compatibility, but
this module no longer performs byte-level hash audits. It validates that
repository links are safe and resolvable, and that removed hash fields do not
re-enter active records.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import yaml

from .domain import repository_ref_error, resolve_repository_ref
from .errors import ValidationFinding

_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)
_REMOVED_HASH_FIELDS = frozenset({"content_" + "sha256", "row_" + "sha256"})

_REFERENCE_ROOTS: tuple[Path, ...] = (
    Path("records/04-candidates"),
    Path("records/05-research"),
    Path("records/06-trades"),
    Path("records/07-reviews"),
    Path("records/_benchmarks"),
    Path("records/_ledger"),
    Path("records/_portfolio-exposure"),
)

_PARSEABLE_REF_SUFFIXES = frozenset({".yaml", ".yml", ".md", ".jsonl"})


def validate_reference_integrity(root: Path) -> list[ValidationFinding]:
    """Validate repository reference links across records."""
    findings: list[ValidationFinding] = []
    for path in discover_snapshot_validation_files(root):
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


def validate_snapshot_integrity(root: Path) -> list[ValidationFinding]:
    """Compatibility wrapper for the ``snapshots`` CLI target."""
    return validate_reference_integrity(root)


def discover_snapshot_validation_files(root: Path) -> list[Path]:
    """Return files participating in repository reference validation."""
    files: list[Path] = []
    for rel_root in _REFERENCE_ROOTS:
        base = root / rel_root
        if not base.exists():
            continue
        files.extend(
            path
            for path in sorted(base.rglob("*"))
            if path.is_file() and path.suffix in {".yaml", ".yml", ".md", ".jsonl"}
        )
    return sorted(set(files))


def _check_nested_refs(
    root: Path,
    target: Path,
    value: Any,
    *,
    prefix: str | None,
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for location, node in _walk_mappings(value, prefix=prefix):
        findings.extend(_check_removed_hash_fields(target, node, location=location))
        findings.extend(_check_repository_ref(root, target, node, location=location))
    return findings


def _check_removed_hash_fields(
    target: Path,
    node: Mapping[str, object],
    *,
    location: str,
) -> list[ValidationFinding]:
    return [
        ValidationFinding(
            severity="error",
            target=target,
            code="reference.removed-hash-field",
            message=f"{field} is no longer allowed in repository links",
            location=f"{location}.{field}" if location else field,
        )
        for field in sorted(_REMOVED_HASH_FIELDS)
        if field in node
    ]


def _check_repository_ref(
    root: Path,
    target: Path,
    node: Mapping[str, object],
    *,
    location: str,
) -> list[ValidationFinding]:
    ref = node.get("ref_path")
    if ref is None:
        return []
    error = repository_ref_error(ref)
    if error is not None:
        return [
            ValidationFinding(
                severity="error",
                target=target,
                code="reference.ref-path",
                message=error,
                location=f"{location}.ref_path",
            )
        ]
    assert isinstance(ref, str)
    ref_path = resolve_repository_ref(root, ref)
    if not ref_path.is_file():
        return [
            ValidationFinding(
                severity="error",
                target=target,
                code="reference.ref-not-found",
                message=f"referenced file does not exist: {ref}",
                location=f"{location}.ref_path",
            )
        ]
    if ref_path.suffix in _PARSEABLE_REF_SUFFIXES:
        parsed = _load_structured_payload(ref_path)
        if isinstance(parsed, ValidationFinding):
            return [
                ValidationFinding(
                    severity="error",
                    target=target,
                    code="reference.ref-parse",
                    message=f"referenced file cannot be parsed: {ref}: {parsed.message}",
                    location=f"{location}.ref_path",
                )
            ]
    return []


def _load_structured_payload(path: Path) -> object | ValidationFinding:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return ValidationFinding(
            severity="error",
            target=path,
            code="reference.io",
            message=f"failed to read file: {exc}",
        )
    if path.suffix == ".jsonl":
        for _line_no, _row, error in _iter_jsonl(path):
            if error is not None:
                return error
        return {}
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
            code="reference.invalid-yaml",
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
                code="reference.io",
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
                    code="reference.invalid-jsonl",
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
                    code="reference.jsonl-non-mapping",
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
