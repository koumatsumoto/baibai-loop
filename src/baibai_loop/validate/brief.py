"""Validate brief YAML artefacts against records/_schemas/brief.json."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from .errors import ValidationFinding

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "records" / "_schemas" / "brief.json"


def _load_validator() -> Draft202012Validator:
    raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected schema root: {SCHEMA_PATH}")
    Draft202012Validator.check_schema(raw)
    return Draft202012Validator(raw)


_VALIDATOR = _load_validator()


def validate_brief_file(path: Path) -> list[ValidationFinding]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="brief.io",
                message=f"failed to read file: {exc}",
            )
        ]
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="brief.invalid-yaml",
                message=f"YAML parse failed: {exc}",
            )
        ]
    if not isinstance(document, dict):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="brief.non-mapping",
                message="brief YAML root must be a mapping",
            )
        ]
    findings: list[ValidationFinding] = []
    for error in _VALIDATOR.iter_errors(document):
        validator_keyword = error.validator or "invalid"
        findings.append(
            ValidationFinding(
                severity="error",
                target=path,
                code=f"brief.{validator_keyword}",
                message=str(error.message),
                location=_format_path(error.absolute_path),
            )
        )
    findings.extend(_check_source_id_references(path, document))
    return findings


def discover_brief_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.yaml") if p.is_file())


def _check_source_id_references(path: Path, document: dict[str, object]) -> list[ValidationFinding]:
    sources = document.get("sources")
    if not isinstance(sources, list):
        return []
    declared_ids: set[str] = set()
    for entry in sources:
        if isinstance(entry, dict):
            entry_id = entry.get("id")
            if isinstance(entry_id, str):
                declared_ids.add(entry_id)
    findings: list[ValidationFinding] = []
    _walk_for_source_ids(path, document, declared_ids, "", findings)
    return findings


def _walk_for_source_ids(
    path: Path,
    node: object,
    declared_ids: set[str],
    location: str,
    findings: list[ValidationFinding],
) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            sub_loc = f"{location}.{key}" if location else str(key)
            if key == "source_ids" and isinstance(value, list):
                for index, ref in enumerate(value):
                    if isinstance(ref, str) and ref not in declared_ids:
                        findings.append(
                            ValidationFinding(
                                severity="error",
                                target=path,
                                code="brief.unknown-source-id",
                                message=(
                                    f"source_ids reference {ref!r} is not declared in sources[]"
                                ),
                                location=f"{sub_loc}[{index}]",
                            )
                        )
            else:
                _walk_for_source_ids(path, value, declared_ids, sub_loc, findings)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _walk_for_source_ids(path, item, declared_ids, f"{location}[{index}]", findings)


def _format_path(parts: Iterable[object]) -> str:
    parts_list = list(parts)
    if not parts_list:
        return ""
    rendered: list[str] = []
    for part in parts_list:
        if isinstance(part, int):
            rendered.append(f"[{part}]")
        else:
            rendered.append(f".{part}" if rendered else str(part))
    return "".join(rendered)
