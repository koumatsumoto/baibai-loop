"""Validate immutable external reference snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .domain import repo_root_for, resolve_ref, sha256_file
from .errors import ValidationFinding


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
                    message="external refs must include ref_path and content_sha256",
                    location=location,
                )
            )
            continue
        ref_path = ref.get("ref_path")
        digest = ref.get("content_sha256")
        if not isinstance(ref_path, str) or not isinstance(digest, str):
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="external-ref.shape",
                    message="external refs must include ref_path and content_sha256",
                    location=location,
                )
            )
            continue
        target = resolve_ref(root, ref_path)
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
            continue
        if sha256_file(target) != digest:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="external-ref.hash",
                    message="external ref content_sha256 does not match file bytes",
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
                    if isinstance(item, Mapping) and (
                        "content_sha256" in item or ref_path.startswith("records/_external/")
                    ):
                        refs.append((f"{location}[{index}]", item))
                    elif isinstance(item, str) and item.startswith("records/_external/"):
                        refs.append((item_location, item))
            else:
                refs.extend(_iter_external_refs(child, location))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            refs.extend(_iter_external_refs(item, f"{prefix}[{index}]"))
    return refs
