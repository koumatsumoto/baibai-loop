"""Validate repository reference links."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from .domain import repository_ref_error, resolve_repository_ref
from .errors import ValidationFinding

_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)
_REFERENCE_ROOTS: tuple[Path, ...] = (Path("records"),)

_PARSEABLE_REF_SUFFIXES = frozenset({".yaml", ".yml", ".md", ".jsonl"})


@dataclass(frozen=True, slots=True)
class _ReferenceSpec:
    prefixes: tuple[str, ...]
    suffixes: tuple[str, ...]
    require_front_matter: bool = False
    required_mapping_keys: tuple[str, ...] = ()
    allow_null: bool = False


_REFERENCE_SPECS: tuple[tuple[str, _ReferenceSpec], ...] = (
    ("playbook_ref", _ReferenceSpec(("records/_playbooks/",), (".md",), True)),
    (
        "input_refs.screening_rules",
        _ReferenceSpec(("records/_config/screening-rules/",), (".yaml", ".yml")),
    ),
    (
        "input_refs.universe",
        _ReferenceSpec(("records/_universe-snapshots/",), (".yaml", ".yml")),
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
_LIST_REFERENCE_FIELDS = frozenset({"source_trade_refs", "source_decision_register_refs"})
_MAPPING_REFERENCE_PARENTS: frozenset[str] = frozenset()
_STRING_LIST_REFERENCE_SPECS: tuple[tuple[str, _ReferenceSpec], ...] = ()
_SCALAR_REFERENCE_SPECS: tuple[tuple[str, _ReferenceSpec], ...] = (
    (
        "candidates_ref",
        _ReferenceSpec(
            ("records/04-candidates/",),
            (".yaml", ".yml"),
            required_mapping_keys=("candidates",),
        ),
    ),
    (
        "macro_context_ref",
        _ReferenceSpec(
            ("records/01-macro-context/",),
            (".yaml", ".yml"),
            required_mapping_keys=("kind", "context_id", "sector_tilts"),
        ),
    ),
    ("research_ref", _ReferenceSpec(("records/05-research/",), (".md",), True, allow_null=True)),
    ("trade_ref", _ReferenceSpec(("records/06-trades/",), (".md",), True, allow_null=True)),
    ("scan_ref", _ReferenceSpec(("records/07-reviews/",), (".yaml", ".yml"))),
    ("ledger_ref", _ReferenceSpec(("records/_ledger/",), (".jsonl",))),
)


def validate_reference_integrity(root: Path) -> list[ValidationFinding]:
    """Validate repository reference links across records."""
    _load_structured_payload.cache_clear()
    _front_matter_payload.cache_clear()
    findings: list[ValidationFinding] = []
    for path in discover_reference_files(root):
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
        findings.extend(_check_standalone_universe_file(root, path, parsed))
        findings.extend(_check_nested_refs(root, path, parsed, prefix=None))
    return findings


def discover_reference_files(root: Path) -> list[Path]:
    """Return files participating in repository reference validation."""
    files: list[Path] = []
    for rel_root in _REFERENCE_ROOTS:
        base = root / rel_root
        if not base.exists():
            continue
        files.extend(
            path
            for path in sorted(base.rglob("*"))
            if path.is_file()
            and path.name != "template.md"
            and path.suffix in {".yaml", ".yml", ".md", ".jsonl"}
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
        findings.extend(_check_reference_field_shapes(target, node, location=location))
        findings.extend(_check_string_list_reference_fields(root, target, node, location=location))
        findings.extend(_check_scalar_reference_fields(root, target, node, location=location))
        findings.extend(_check_repository_ref(root, target, node, location=location))
    return findings


def _check_repository_ref(
    root: Path,
    target: Path,
    node: Mapping[str, object],
    *,
    location: str,
) -> list[ValidationFinding]:
    ref = node.get("ref_path")
    if ref is None:
        if _spec_for_location(location) is not None:
            return [
                ValidationFinding(
                    severity="error",
                    target=target,
                    code="reference.ref-shape",
                    message="repository ref mapping must include ref_path",
                    location=f"{location}.ref_path",
                )
            ]
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
        if not isinstance(parsed, Mapping):
            return [
                ValidationFinding(
                    severity="error",
                    target=target,
                    code="reference.ref-parse",
                    message=f"referenced file root must be a mapping: {ref}",
                    location=f"{location}.ref_path",
                )
            ]
    return []


def _check_reference_field_shapes(
    target: Path,
    node: Mapping[str, object],
    *,
    location: str,
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for field, value in node.items():
        if field == "ref_path":
            continue
        child_location = f"{location}.{field}" if location else str(field)
        if field in _MAPPING_REFERENCE_PARENTS:
            if not isinstance(value, Mapping):
                findings.append(
                    ValidationFinding(
                        severity="error",
                        target=target,
                        code="reference.ref-shape",
                        message=f"{field} must be a repository ref mapping container",
                        location=child_location,
                    )
                )
            continue
        if field in _LIST_REFERENCE_FIELDS:
            if not isinstance(value, list):
                findings.append(
                    ValidationFinding(
                        severity="error",
                        target=target,
                        code="reference.ref-shape",
                        message=f"{field} must be a list of repository ref mappings",
                        location=child_location,
                    )
                )
                continue
            for index, item in enumerate(value):
                if not isinstance(item, Mapping):
                    findings.append(
                        ValidationFinding(
                            severity="error",
                            target=target,
                            code="reference.ref-shape",
                            message=f"{field} entries must be repository ref mappings",
                            location=f"{child_location}[{index}]",
                        )
                    )
                    continue
                if "ref_path" not in item:
                    findings.append(
                        ValidationFinding(
                            severity="error",
                            target=target,
                            code="reference.ref-shape",
                            message=f"{field} entries must include ref_path",
                            location=f"{child_location}[{index}].ref_path",
                        )
                    )
            continue
        if field == "source_refs":
            continue
        if _is_reference_container_location(child_location) and not isinstance(value, Mapping):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=target,
                    code="reference.ref-shape",
                    message=f"{field} must be a repository ref mapping",
                    location=child_location,
                )
            )
    return findings


def _check_scalar_reference_fields(
    root: Path,
    target: Path,
    node: Mapping[str, object],
    *,
    location: str,
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for field, value in node.items():
        spec = _scalar_spec_for_field(field)
        if spec is None:
            continue
        child_location = f"{location}.{field}" if location else str(field)
        if value is None and spec.allow_null:
            continue
        if not isinstance(value, str):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=target,
                    code="reference.ref-shape",
                    message=f"{field} must be a repository-relative string path",
                    location=child_location,
                )
            )
            continue
        findings.extend(_check_scalar_repository_ref(root, target, value, child_location, spec))
    return findings


def _check_string_list_reference_fields(
    root: Path,
    target: Path,
    node: Mapping[str, object],
    *,
    location: str,
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for field, value in node.items():
        spec = _string_list_spec_for_field(target, field)
        if spec is None:
            continue
        child_location = f"{location}.{field}" if location else str(field)
        if value is None:
            continue
        if not isinstance(value, list):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=target,
                    code="reference.ref-shape",
                    message=f"{field} must be a list of repository-relative string paths",
                    location=child_location,
                )
            )
            continue
        for index, item in enumerate(value):
            item_location = f"{child_location}[{index}]"
            if not isinstance(item, str):
                findings.append(
                    ValidationFinding(
                        severity="error",
                        target=target,
                        code="reference.ref-shape",
                        message=f"{field} entries must be repository-relative string paths",
                        location=item_location,
                    )
                )
                continue
            findings.extend(_check_scalar_repository_ref(root, target, item, item_location, spec))
    return findings


def _check_scalar_repository_ref(
    root: Path,
    target: Path,
    ref: str,
    location: str,
    spec: _ReferenceSpec,
) -> list[ValidationFinding]:
    error = repository_ref_error(ref, root=root)
    if error is not None:
        return [
            ValidationFinding(
                severity="error",
                target=target,
                code="reference.ref-path",
                message=error,
                location=location,
            )
        ]
    ref_path = resolve_repository_ref(root, ref)
    if not ref.startswith(spec.prefixes):
        prefixes = ", ".join(spec.prefixes)
        return [
            ValidationFinding(
                severity="error",
                target=target,
                code="reference.ref-prefix",
                message=f"reference must point under {prefixes}: {ref}",
                location=location,
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
                location=location,
            )
        ]
    if not ref_path.is_file():
        return [
            ValidationFinding(
                severity="error",
                target=target,
                code="reference.ref-not-found",
                message=f"referenced file does not exist: {ref}",
                location=location,
            )
        ]
    parsed = _load_structured_payload(ref_path)
    if isinstance(parsed, ValidationFinding):
        return [
            ValidationFinding(
                severity="error",
                target=target,
                code="reference.ref-parse",
                message=f"referenced file cannot be parsed: {ref}: {parsed.message}",
                location=location,
            )
        ]
    shape_findings = _check_referenced_payload_shape(
        target,
        parsed,
        ref,
        location,
        spec,
    )
    if shape_findings:
        return shape_findings
    return []


def _check_referenced_payload_shape(
    target: Path,
    payload: object,
    ref: str,
    location: str,
    spec: _ReferenceSpec,
) -> list[ValidationFinding]:
    if not isinstance(payload, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=target,
                code="reference.ref-parse",
                message=f"referenced file root must be a mapping: {ref}",
                location=location,
            )
        ]
    missing = [key for key in spec.required_mapping_keys if key not in payload]
    if missing:
        return [
            ValidationFinding(
                severity="error",
                target=target,
                code="reference.ref-parse",
                message=f"referenced file is missing required root keys: {', '.join(missing)}",
                location=location,
            )
        ]
    return []


def _is_reference_container_location(location: str) -> bool:
    normalized = location.replace("[", ".").replace("]", "")
    for marker, _spec in _REFERENCE_SPECS:
        if marker == "source_refs":
            continue
        if normalized == marker or normalized.endswith(f".{marker}"):
            return True
    return False


def _scalar_spec_for_field(field: str) -> _ReferenceSpec | None:
    for marker, spec in _SCALAR_REFERENCE_SPECS:
        if field == marker:
            return spec
    return None


def _string_list_spec_for_field(target: Path, field: str) -> _ReferenceSpec | None:
    for marker, spec in _STRING_LIST_REFERENCE_SPECS:
        if field == marker:
            return spec
    return None


def _location_has_marker(location: str, marker: str) -> bool:
    normalized = location.replace("[", ".").replace("]", "")
    return normalized == marker or normalized.endswith(f".{marker}") or f".{marker}." in normalized


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
        if (
            normalized == marker
            or normalized.endswith(f".{marker}")
            or normalized.startswith(f"{marker}.")
            or f".{marker}." in normalized
        ):
            return spec
    return None


def _check_standalone_universe_file(
    root: Path, path: Path, payload: object
) -> list[ValidationFinding]:
    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        relative = path.as_posix()
    if not relative.startswith("records/_universe-snapshots/"):
        return []
    if not isinstance(payload, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="reference.universe-shape",
                message="universe snapshot must be a YAML mapping",
            )
        ]
    members = payload.get("members")
    members_recorded = payload.get("members_recorded")
    universe_size = payload.get("universe_size")
    members_scope = payload.get("members_scope")
    if not isinstance(universe_size, int) or universe_size < 0:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="reference.universe-shape",
                message="universe_size must be a non-negative integer",
                location="universe_size",
            )
        ]
    if members_scope not in {None, "full_universe", "candidates", "not_recorded"}:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="reference.universe-shape",
                message="members_scope must be full_universe, candidates, or not_recorded",
                location="members_scope",
            )
        ]
    if not isinstance(members, list) or members_recorded != len(members):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="reference.universe-members",
                message="members_recorded must equal members length",
                location="members_recorded",
            )
        ]
    if members_scope in {None, "full_universe"} and len(members) != universe_size:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="reference.universe-members",
                message="full_universe members length must equal universe_size",
                location="members",
            )
        ]
    if members_scope == "not_recorded" and members:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="reference.universe-members",
                message="not_recorded universe snapshots must not include members",
                location="members",
            )
        ]
    seen_tickers: set[str] = set()
    for index, member in enumerate(members):
        if not isinstance(member, Mapping) or not isinstance(member.get("ticker"), str):
            return [
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="reference.universe-members",
                    message="universe members must be mappings with ticker",
                    location=f"members[{index}]",
                )
            ]
        ticker = member["ticker"]
        if ticker in seen_tickers:
            return [
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="reference.universe-members",
                    message=f"universe member ticker must be unique: {ticker}",
                    location=f"members[{index}].ticker",
                )
            ]
        seen_tickers.add(ticker)
    return []


@cache
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
        if match is None:
            if text.startswith("---\n"):
                return ValidationFinding(
                    severity="error",
                    target=path,
                    code="reference.invalid-yaml",
                    message="Markdown front matter is not closed",
                )
            return {}
        try:
            front_matter: object = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            return ValidationFinding(
                severity="error",
                target=path,
                code="reference.invalid-yaml",
                message=f"Markdown front matter YAML parse failed: {exc}",
            )
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


@cache
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
