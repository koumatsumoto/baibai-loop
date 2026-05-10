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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .domain import repository_ref_error, resolve_repository_ref
from .errors import ValidationFinding

_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)
_REMOVED_HASH_FIELDS = frozenset({"content_" + "sha256", "row_" + "sha256"})

_REFERENCE_ROOTS: tuple[Path, ...] = (Path("records"),)

_PARSEABLE_REF_SUFFIXES = frozenset({".yaml", ".yml", ".md", ".jsonl"})


@dataclass(frozen=True, slots=True)
class _ReferenceSpec:
    prefixes: tuple[str, ...]
    suffixes: tuple[str, ...]
    require_front_matter: bool = False


_REFERENCE_SPECS: tuple[tuple[str, _ReferenceSpec], ...] = (
    ("playbook_ref", _ReferenceSpec(("records/_playbooks/",), (".md",), True)),
    ("policy_ref", _ReferenceSpec(("records/01-policy/",), (".md",), True)),
    (
        "portfolio_exposure_ref",
        _ReferenceSpec(("records/_portfolio-exposure/",), (".yaml", ".yml")),
    ),
    (
        "calendar_refs.business_days",
        _ReferenceSpec(("records/_calendars/business-days/",), (".yaml", ".yml")),
    ),
    (
        "calendar_refs.events",
        _ReferenceSpec(("records/_calendars/events/",), (".yaml", ".yml")),
    ),
    (
        "calendar_refs.corporate_actions",
        _ReferenceSpec(("records/_calendars/corporate-actions/",), (".yaml", ".yml")),
    ),
    (
        "universe_ref",
        _ReferenceSpec(("records/_universe-snapshots/",), (".yaml", ".yml")),
    ),
    ("market_data_ref", _ReferenceSpec(("records/_market-data/",), (".yaml", ".yml"))),
    ("source_refs", _ReferenceSpec(("records/_external/",), (".md",))),
    ("source_trade_refs", _ReferenceSpec(("records/06-trades/",), (".md",), True)),
    (
        "source_decision_register_refs",
        _ReferenceSpec(("records/_ledger/",), (".jsonl",)),
    ),
)


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
    error = repository_ref_error(ref, root=root)
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
    findings = _check_reference_spec(target, ref, ref_path, location)
    if findings:
        return findings
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


def _check_reference_spec(
    target: Path,
    ref: str,
    ref_path: Path,
    location: str,
) -> list[ValidationFinding]:
    spec = _spec_for_location(location)
    if spec is None:
        if ref_path.suffix not in _PARSEABLE_REF_SUFFIXES:
            return [
                ValidationFinding(
                    severity="error",
                    target=target,
                    code="reference.ref-suffix",
                    message=f"repository reference must point to a parseable record file: {ref}",
                    location=f"{location}.ref_path",
                )
            ]
        return []
    if not ref.startswith(spec.prefixes):
        prefixes = ", ".join(spec.prefixes)
        return [
            ValidationFinding(
                severity="error",
                target=target,
                code="reference.ref-prefix",
                message=f"reference must point under {prefixes}: {ref}",
                location=f"{location}.ref_path",
            )
        ]
    if ref_path.suffix not in spec.suffixes:
        suffixes = ", ".join(spec.suffixes)
        return [
            ValidationFinding(
                severity="error",
                target=target,
                code="reference.ref-suffix",
                message=f"reference must use suffix {suffixes}: {ref}",
                location=f"{location}.ref_path",
            )
        ]
    if spec.require_front_matter and _front_matter_payload(ref_path) is None:
        return [
            ValidationFinding(
                severity="error",
                target=target,
                code="reference.ref-front-matter",
                message=f"referenced markdown file must have YAML front matter: {ref}",
                location=f"{location}.ref_path",
            )
        ]
    return []


def _spec_for_location(location: str) -> _ReferenceSpec | None:
    normalized = location.replace("[", ".").replace("]", "")
    for marker, spec in _REFERENCE_SPECS:
        if marker in normalized:
            return spec
    return None


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
        front_matter = _front_matter_payload(path)
        if front_matter is None:
            return {}
        return front_matter
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


def _front_matter_payload(path: Path) -> object | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return None
    try:
        loaded: object = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
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
