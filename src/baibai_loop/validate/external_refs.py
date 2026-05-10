"""Validate external repository references."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .domain import repo_root_for, repository_ref_error, resolve_repository_ref
from .errors import ValidationFinding

_CONTENT_HASH_FIELD = "content_" + "sha256"
_ROW_HASH_FIELD = "row_" + "sha256"


def validate_external_refs_file(path: Path, payload: Mapping[str, Any]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    root = repo_root_for(path)
    for location, ref in _iter_external_refs(payload):
        if not isinstance(ref, Mapping):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="external-ref.shape",
                    message="external refs must be mapping objects",
                    location=location,
                )
            )
            continue
        ref_path = ref.get("ref_path")
        if _CONTENT_HASH_FIELD in ref or _ROW_HASH_FIELD in ref:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="external-ref.removed-hash-field",
                    message="external refs must not include removed hash fields",
                    location=location,
                )
            )
        error = repository_ref_error(ref_path)
        if error is not None:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="external-ref.shape",
                    message=error,
                    location=location,
                )
            )
            continue
        assert isinstance(ref_path, str)
        target = resolve_repository_ref(root, ref_path)
        if not target.is_file():
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="external-ref.missing",
                    message=f"external ref file does not exist: {ref_path}",
                    location=location,
                )
            )
    return findings


def _iter_external_refs(value: object, prefix: str = "") -> list[tuple[str, object]]:
    refs: list[tuple[str, object]] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            location = f"{prefix}.{key}" if prefix else str(key)
            if key in {"external_refs", "source_refs"} and isinstance(child, list):
                for index, item in enumerate(child):
                    item_location = f"{location}[{index}]"
                    if key == "external_refs":
                        refs.append((item_location, item))
                        continue
                    ref_path = str(item.get("ref_path", "")) if isinstance(item, Mapping) else ""
                    if isinstance(item, Mapping) and ref_path.startswith("records/_external/"):
                        refs.append((f"{location}[{index}]", item))
                    elif isinstance(item, str) and item.startswith("records/_external/"):
                        refs.append((item_location, item))
            else:
                refs.extend(_iter_external_refs(child, location))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            refs.extend(_iter_external_refs(item, f"{prefix}[{index}]"))
    return refs
