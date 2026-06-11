"""Reference-list checks: repository refs must exist and be repo-relative."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from baibai_loop.validate.domain import (
    load_reference_mapping,
    repo_root_for,
    repository_ref_error,
    resolve_repository_ref,
)
from baibai_loop.validate.errors import ValidationFinding


def _check_reference_refs(
    path: Path, front_matter: Mapping[str, object]
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    specs = {
        "playbook_ref": (("records/_playbooks/",), (".md",)),
    }
    for field, (prefixes, suffixes) in specs.items():
        value = front_matter.get(field)
        findings.extend(
            _check_repository_ref(
                path,
                value,
                location=field,
                code="research.reference-ref",
                prefixes=prefixes,
                suffixes=suffixes,
            )
        )
    return findings


def _check_repository_ref(
    path: Path,
    value: object,
    *,
    location: str,
    code: str,
    prefixes: tuple[str, ...],
    suffixes: tuple[str, ...],
) -> list[ValidationFinding]:
    if not isinstance(value, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code=code,
                message=f"{location} must be a repository ref mapping",
                location=location,
            )
        ]
    findings: list[ValidationFinding] = []
    root = repo_root_for(path)
    ref = value.get("ref_path")
    error = repository_ref_error(ref, root=root)
    if error is not None:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=code,
                message=error,
                location=f"{location}.ref_path",
            )
        )
        return findings
    assert isinstance(ref, str)
    ref_path = resolve_repository_ref(root, ref)
    if not ref_path.is_file():
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=code,
                message=f"referenced file does not exist: {ref}",
                location=f"{location}.ref_path",
            )
        )
        return findings
    if not ref.startswith(prefixes):
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=code,
                message=f"{location}.ref_path must point under {', '.join(prefixes)}",
                location=f"{location}.ref_path",
            )
        )
    if ref_path.suffix not in suffixes:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=code,
                message=f"{location}.ref_path must use suffix {', '.join(suffixes)}",
                location=f"{location}.ref_path",
            )
        )
        return findings
    try:
        if ref_path.suffix == ".md":
            load_reference_mapping(root, value)
        else:
            loaded = yaml.safe_load(ref_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, Mapping):
                raise ValueError("referenced YAML must be a mapping")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=code,
                message=f"referenced file cannot be parsed: {exc}",
                location=f"{location}.ref_path",
            )
        )
    return findings
